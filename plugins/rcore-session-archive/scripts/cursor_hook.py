#!/usr/bin/env python3
"""Project-local Cursor hooks that save mode-filtered JSONL conversations."""

import json
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from archive_session import (
    ARCHIVE_DIR_NAME, find_project_root, read_archive_config, archive_session_id,
    prepare_archive_directory, existing_archive, session_archive_path,
)
from archive_storage import (
    atomic_write, digest, event_row, open_index, private_directory,
    put_entry, read_entries, text_event_id, write_jsonl,
)


EVENTS = (
    "beforeSubmitPrompt", "afterAgentResponse", "afterAgentThought",
    "preToolUse", "postToolUse", "postToolUseFailure", "stop",
)
COMMAND = 'python3 ".cursor/rcore-hooks/cursor_hook.py"'


def warn(message):
    print(f"[rCore Cursor] {message}", file=sys.stderr)


def handle(payload, project_root=None):
    if not isinstance(payload, dict) or payload.get("hook_event_name") not in EVENTS:
        return
    root = project_root or find_project_root(str(Path.cwd()), "cursor")
    if root is None:
        return
    root = Path(root).resolve()
    # A multi-root workspace containing another project is intentionally excluded.
    roots = payload.get("workspace_roots")
    if not isinstance(roots, list) or not roots:
        return
    for workspace in roots:
        if not isinstance(workspace, str) or not Path(workspace).is_absolute():
            return
        resolved = Path(workspace).resolve()
        if resolved != root and root not in resolved.parents:
            return
    config = read_archive_config(root, "cursor")
    if config is None:
        return
    mode = config["mode"]
    conversation, generation = payload.get("conversation_id"), payload.get("generation_id")
    if not isinstance(conversation, str) or not conversation or not isinstance(generation, str) or not generation:
        return
    event = payload["hook_event_name"]
    sid = archive_session_id(conversation)
    if sid is None:
        return
    directory = root / ARCHIVE_DIR_NAME / "cursor"
    index = directory / ".state" / f"{sid}.sqlite3"
    if existing_archive(directory, sid) is not None and not index.exists():
        raise ValueError("archive index is missing; preserve the existing JSONL")
    if event == "stop" and not index.exists():
        return  # Never create an empty turn from a lifecycle event.
    prepare_archive_directory(root, "cursor")
    private_directory(index.parent)
    turn_id = digest(conversation, generation)
    now = time.time_ns()
    with closing(open_index(index)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            first = db.execute("SELECT MIN(started) FROM turns").fetchone()[0]
            started = datetime.fromtimestamp((first or now) / 1_000_000_000, timezone.utc).isoformat()
            archive_path = session_archive_path(directory, sid, started)
            entries = read_entries(archive_path, "cursor", conversation)
            db.execute(
                "INSERT OR IGNORE INTO turns(id,ordinal,started) VALUES(?,(SELECT COALESCE(MAX(ordinal),0)+1 FROM turns),?)",
                (turn_id, now),
            )
            turn = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()

            def demote_reply(key):
                db.execute("UPDATE events SET kind='intermediate' WHERE id=?", (key,))
                if key in entries:
                    entries[key] = {**entries[key], "type": "intermediate"}

            if event == "beforeSubmitPrompt":
                prompt = payload.get("prompt", "")
                if not isinstance(prompt, str):
                    raise ValueError("invalid prompt")
                attachments = payload.get("attachments", [])
                references = [str(item["file_path"]) for item in attachments
                              if isinstance(item, dict) and item.get("file_path")]
                if references:
                    prompt += "\n\n附件引用：\n" + "\n".join(f"- {path}" for path in references)
                if prompt.strip():
                    key = digest(turn_id, "user")
                    event_row(db, turn, key, "user", digest(prompt), now)
                    put_entry(entries, key, {"type": "message", "role": "user", "content": prompt})

            elif event in {"preToolUse", "postToolUse", "postToolUseFailure"}:
                tool_id = payload.get("tool_use_id")
                if not isinstance(tool_id, str) or not tool_id:
                    raise ValueError("Cursor tool_use_id is missing; update Cursor")
                key = digest(turn_id, "tool", tool_id)
                previous = db.execute("SELECT * FROM events WHERE id=?", (key,)).fetchone()
                if previous is None:
                    db.execute("UPDATE turns SET phase=phase+1 WHERE id=?", (turn_id,))
                # A message followed by another tool is commentary, not the final answer.
                if previous is None and turn["reply"]:
                    demote_reply(turn["reply"])
                    db.execute("UPDATE turns SET reply='' WHERE id=?", (turn_id,))
                name = str(payload.get("tool_name") or "Tool")
                call = {"type": "tool_call", "name": name, "call_id": tool_id, "input": payload.get("tool_input")}
                result = {"type": "tool_result", "call_id": tool_id, "content": payload.get("tool_output"),
                          "is_error": event == "postToolUseFailure"}
                error = str(payload.get("error_message") or payload.get("failure_type") or "工具执行失败") if result["is_error"] else ""
                if error:
                    result["content"] = error
                note = payload.get("agent_message")
                if isinstance(note, str) and note.strip():
                    note_key = digest(key, "note")
                    event_row(db, turn, note_key, "intermediate", digest(note), now)
                    if mode == "full":
                        put_entry(entries, note_key, {"type": "intermediate", "content": note})
                # Do not let a replayed pre hook overwrite an already completed tool.
                if not (event == "preToolUse" and previous and previous["kind"] == "tool"):
                    kind = "tool_pending" if event == "preToolUse" else "tool"
                    duration = payload.get("duration", 0)
                    duration = int(max(0, duration) * 1_000_000) if isinstance(duration, (int, float)) else 0
                    event_row(db, turn, key, kind, digest(call, result), now, duration)
                    db.execute("UPDATE events SET kind=? WHERE id=?", (kind, key))
                    if mode != "messages":
                        put_entry(entries, key, call)
                    if event != "preToolUse":
                        result_key = digest(key, "result")
                        event_row(db, turn, result_key, "tool_result", digest(result), now)
                        if mode == "full":
                            put_entry(entries, result_key, result)

            elif event in {"afterAgentResponse", "afterAgentThought"}:
                text = payload.get("text")
                if isinstance(text, str) and text.strip():
                    key = text_event_id(db, turn, event, text)
                    if event == "afterAgentResponse":
                        if turn["reply"] and turn["reply"] != key:
                            demote_reply(turn["reply"])
                        event_row(db, turn, key, "reply", digest(text), now)
                        db.execute("UPDATE turns SET reply=? WHERE id=?", (key, turn_id))
                        put_entry(entries, key, {"type": "message", "role": "assistant", "content": text})
                    else:
                        event_row(db, turn, key, "reasoning", digest(text), now)
                        if mode == "full":
                            put_entry(entries, key, {"type": "reasoning", "content": text})

            write_jsonl(archive_path, db, entries, mode, conversation, "cursor")


def install_hooks(root):
    path = Path(root) / ".cursor/hooks.json"
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Cursor project configuration must not be a symlink")
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "hooks": {}}
    if not isinstance(config, dict) or config.get("version", 1) != 1:
        raise ValueError("unsupported Cursor hooks configuration")
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Cursor hooks must be an object")
    config.setdefault("version", 1)
    for event in EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            raise ValueError(f"invalid Cursor hook list: {event}")
        # Preserve all unrelated hooks; repeat setup must not add duplicate handlers.
        hooks[event] = [entry for entry in entries if entry.get("command") != COMMAND] + [{"command": COMMAND}]
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the native hook AND its runtime in the ignored project directory.
    # A checkout of a lab branch must not remove the installed Cursor integration.
    runtime = path.parent / "rcore-hooks"
    private_directory(runtime)
    for filename in ("archive_session.py", "archive_storage.py", "cursor_hook.py"):
        source = Path(__file__).resolve().parent / filename
        atomic_write(runtime / filename, source.read_text(encoding="utf-8"))
    atomic_write(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--install":
        install_hooks(Path(sys.argv[2]).resolve())
        return 0
    try:
        handle(json.load(sys.stdin))
    except Exception as error:
        # Fail open without leaking any content or modifying a permission decision.
        warn(f"hook 未完成（{type(error).__name__}）；请检查项目配置及归档目录权限。")
    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
