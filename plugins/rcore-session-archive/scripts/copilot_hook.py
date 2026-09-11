#!/usr/bin/env python3
"""VS Code Copilot project hooks: transcript v1 to local JSONL.

The transcript contract is preview, not a stable API. Check the session header,
never scan workspaceStorage, and fail open without replacing archives on errors.
Use user-message ordinals, not assistant.turn_start IDs (which count LLM rounds).
Only JSONL bodies and a content-free ordering index are persisted.
"""

import json
import re
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
    put_entry, read_entries, write_jsonl,
)


AGENT = "vscode-copilot"
EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact", "Stop")
COMMAND = 'python3 ".vscode/rcore-hooks/copilot_hook.py"'
HOOK_LOCATION = ".github/hooks/rcore-session-archive.json"


class TranscriptError(ValueError):
    pass


def warn(message):
    print(f"[rCore VS Code Copilot] {message}", file=sys.stderr)


def timestamp_ns(value):
    if not isinstance(value, str):
        raise TranscriptError("transcript 时间戳缺失。")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("missing timezone")
        return int(stamp.timestamp() * 1_000_000_000)
    except (ValueError, OverflowError):
        raise TranscriptError("transcript 时间戳格式暂不支持。") from None


def tool_id(value):
    if not isinstance(value, str) or not value:
        raise TranscriptError("工具调用 ID 缺失。")
    # The official transcript writer removes this internal suffix, hook inputs may not.
    return value.split("__vscode-", 1)[0]


def read_records(path, session_id):
    if not path.is_absolute() or not path.is_file():
        raise TranscriptError("transcript 尚不可读；请检查 Copilot Hooks 日志。")
    records, seen = [], {}
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                raise TranscriptError("transcript 包含未写完或损坏的记录，保留原留档。") from None
            if not isinstance(record, dict) or not isinstance(record.get("data"), dict):
                raise TranscriptError("暂不支持此 transcript 格式，请检查 VS Code 与课程适配器版本。")
            identity = record.get("id")
            if not isinstance(identity, str) or not identity:
                raise TranscriptError("transcript 记录 ID 缺失。")
            if identity in seen:
                if seen[identity] != record:
                    raise TranscriptError("transcript 出现冲突的记录 ID，保留原留档。")
                continue
            seen[identity] = record
            records.append(record)
    if not records:
        raise TranscriptError("transcript 尚未写入会话头。")
    header = records[0]
    data = header["data"]
    if header.get("type") != "session.start" or data.get("version") != 1:
        raise TranscriptError("暂不支持此 transcript 版本（当前支持 v1），原留档未覆盖。")
    if data.get("sessionId") != session_id:
        raise TranscriptError("hook 与 transcript 的会话 ID 不匹配，已跳过采集。")
    return records


def new_tool(turn, identity, name, arguments, stamp):
    identity = tool_id(identity)
    tools = turn["tools"]
    if identity not in tools:
        call = {"type": "tool_call", "name": str(name or "Tool"), "call_id": identity, "input": arguments}
        item = {"key": f"tool:{identity}", "kind": "tool_pending", "event": call, "started": stamp, "ended": stamp, "result": None}
        tools[identity] = item
        turn["events"].append(item)
    else:
        item = tools[identity]
        item["event"].update(name=str(name or item["event"]["name"]), input=arguments)
    return item


def set_result(turn, identity, content, stamp, failed=False):
    identity = tool_id(identity)
    item = turn["tools"].get(identity)
    if item is None:
        raise TranscriptError("工具结果缺少对应调用，原留档未覆盖。")
    result = {"type": "tool_result", "call_id": identity, "content": content, "is_error": failed}
    if item["result"] is None:
        turn["events"].append({"key": f"result:{identity}", "kind": "tool_result", "event": result, "started": stamp, "ended": stamp})
    else:
        for row in turn["events"]:
            if row["key"] == f"result:{identity}":
                row.update(event=result, ended=stamp)
                break
    item.update(kind="tool", ended=stamp, result=result)


def parse_turns(records):
    turns = []
    turn = None
    for record in records[1:]:
        kind, data = record.get("type"), record["data"]
        if kind not in {"user.message", "assistant.message", "assistant.turn_start", "assistant.turn_end", "tool.execution_start", "tool.execution_complete"}:
            continue  # Usage, transport, compaction and other metadata are not messages.
        stamp = timestamp_ns(record.get("timestamp"))
        if kind == "user.message":
            content = data.get("content")
            if not isinstance(content, str):
                raise TranscriptError("暂不支持此用户消息格式。")
            attachments = data.get("attachments", [])
            if isinstance(attachments, list):
                paths = [item.get("file_path") or item.get("filePath") or item.get("path")
                         for item in attachments if isinstance(item, dict)]
                paths = [path for path in paths if isinstance(path, str)]
                if paths:
                    content += "\n\n附件引用：\n" + "\n".join(f"- {path}" for path in paths)
            turn = {"number": len(turns) + 1, "prompt": content, "raw_prompt": data["content"], "started": stamp,
                    "ended": stamp, "events": [], "tools": {}, "reply": None, "complete": False, "messages": 0}
            turns.append(turn)
        elif turn is not None:
            turn["ended"] = max(stamp, turn["ended"])
            if kind == "assistant.turn_start":
                turn["complete"] = False
                turn["reply"] = None
            elif kind == "assistant.turn_end":
                turn["complete"] = True
            elif kind == "assistant.message":
                content, requests = data.get("content"), data.get("toolRequests")
                if not isinstance(content, str) or not isinstance(requests, list):
                    raise TranscriptError("暂不支持此 assistant.message 格式。")
                turn["messages"] += 1
                index = turn["messages"]
                reasoning = data.get("reasoningText")
                if isinstance(reasoning, str) and reasoning.strip():
                    turn["events"].append({"key": f"reasoning:{index}", "kind": "reasoning", "event": {"type": "reasoning", "content": reasoning}, "started": stamp, "ended": stamp})
                turn["reply"] = None
                if content.strip():
                    item = {"key": f"assistant:{index}", "kind": "intermediate", "event": {"type": "intermediate", "content": content}, "started": stamp, "ended": stamp}
                    turn["events"].append(item)
                    if not requests:
                        turn["reply"] = item
                for request in requests:
                    if not isinstance(request, dict):
                        raise TranscriptError("工具请求格式不受支持。")
                    new_tool(turn, request.get("toolCallId"), request.get("name"), request.get("arguments"), stamp)
            elif kind == "tool.execution_start":
                turn["reply"] = None
                new_tool(turn, data.get("toolCallId"), data.get("toolName"), data.get("arguments"), stamp)
            elif kind == "tool.execution_complete":
                result = data.get("result", {})
                if not isinstance(result, dict):
                    raise TranscriptError("工具结果格式不受支持。")
                set_result(turn, data.get("toolCallId"), result.get("content"), stamp, data.get("success") is False)
    return turns


def snapshot(path, payload, attempts=5):
    last_error = None
    for attempt in range(attempts):
        try:
            records = read_records(path, payload["session_id"])
            turns = parse_turns(records)
            if payload["hook_event_name"] == "UserPromptSubmit":
                if not turns or turns[-1]["raw_prompt"] != payload.get("prompt"):
                    raise TranscriptError("最新用户输入尚未写入 transcript，暂不采集旧轮次。")
            if payload["hook_event_name"] == "Stop" and turns and not turns[-1]["complete"]:
                raise TranscriptError("最新回复尚未完整写入 transcript，保留原留档，等待后续 hook 刷新。")
            return records, turns
        except (TranscriptError, OSError, UnicodeError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(0.2)
    if isinstance(last_error, TranscriptError):
        raise last_error
    raise TranscriptError("无法读取 transcript，原留档未覆盖。") from None


def handle(payload, project_root=None):
    if not isinstance(payload, dict) or payload.get("hook_event_name") not in EVENTS:
        return
    # Never interpret a Claude/Cursor hook merely because it has similarly named fields.
    if payload.get("hook_event_name") == "SessionStart":
        return  # The first user entry is not yet logged here. No empty archive/history import.
    root = project_root or find_project_root(str(Path.cwd()), AGENT)
    if root is None:
        return
    root = Path(root).resolve()
    cwd = payload.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            return
        resolved = Path(cwd).resolve()
        if resolved != root and root not in resolved.parents:
            return
    config = read_archive_config(root, AGENT)
    if config is None:
        return
    mode = config["mode"]
    session_id = payload.get("session_id")
    transcript = payload.get("transcript_path")
    if not isinstance(session_id, str) or not session_id or not isinstance(transcript, str) or not transcript:
        raise TranscriptError("缺少 session_id / transcript_path，请检查是否为受支持的 VS Code Copilot 本地 Agent 会话。")
    records, turns = snapshot(Path(transcript), payload)
    hook_stamp = timestamp_ns(payload.get("timestamp"))
    context = records[0]["data"].get("context", {}) or {}
    context_cwd = context.get("cwd") if isinstance(context, dict) else None
    if context_cwd:
        if not isinstance(context_cwd, str) or not Path(context_cwd).is_absolute():
            return
        resolved = Path(context_cwd).resolve()
        if resolved != root and root not in resolved.parents:
            return
    if not turns:
        return
    current = turns[-1]
    event = payload["hook_event_name"]
    if event in {"PreToolUse", "PostToolUse"}:
        stamp = timestamp_ns(payload.get("timestamp"))
        identity = tool_id(payload.get("tool_use_id"))
        unseen_tool = identity not in current["tools"]
        item = new_tool(current, identity, payload.get("tool_name"), payload.get("tool_input"), stamp)
        if unseen_tool:
            current["reply"] = None
            current["complete"] = False
        if event == "PostToolUse" and item["result"] is None:
            set_result(current, identity, payload.get("tool_response"), stamp)
        current["ended"] = max(current["ended"], stamp)

    sid = archive_session_id(session_id)
    if sid is None:
        return
    directory = root / ARCHIVE_DIR_NAME / AGENT
    index = directory / ".state" / f"{sid}.sqlite3"
    if existing_archive(directory, sid) is not None and not index.exists():
        raise TranscriptError("归档索引缺失；请与 JSONL 一起恢复 .state，原文件未覆盖。")
    prepare_archive_directory(root, AGENT)
    private_directory(index.parent)
    with closing(open_index(index)) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS copilot_source (
                turn_id TEXT PRIMARY KEY, source_number INTEGER, prompt_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS copilot_watermark (
                id INTEGER PRIMARY KEY CHECK(id=1), record_count INTEGER,
                hook_stamp INTEGER, source_header TEXT
            );
        """)
        with db:
            db.execute("BEGIN IMMEDIATE")
            watermark = db.execute("SELECT * FROM copilot_watermark WHERE id=1").fetchone()
            if watermark and hook_stamp < watermark["hook_stamp"]:
                return  # A concurrent older hook must not roll a newer archive back.
            if watermark and len(records) < watermark["record_count"]:
                # Copilot can recreate an expired source from chat history with new
                # UUIDs and without tool execution/result entries. This is not a
                # stale snapshot, and completed local tool blocks must survive it.
                previous_turn = db.execute("SELECT MAX(source_number) FROM copilot_source").fetchone()[0]
                if records[0]["id"] == watermark["source_header"] or len(turns) < previous_turn:
                    raise TranscriptError("transcript 出现截断，保留原留档；若持续出现，请新建会话。")
            started = datetime.fromtimestamp(current["started"] / 1_000_000_000, timezone.utc).isoformat()
            archive = session_archive_path(directory, sid, started)
            entries = read_entries(archive, AGENT, session_id)
            for source_turn in turns:
                identity = digest(AGENT, session_id, source_turn["number"])
                turn = db.execute("SELECT * FROM turns WHERE id=?", (identity,)).fetchone()
                known = db.execute("SELECT * FROM copilot_source WHERE turn_id=?", (identity,)).fetchone()
                prompt_hash = digest(source_turn["raw_prompt"])
                if known and known["prompt_hash"] != prompt_hash:
                    raise TranscriptError("transcript 已重排或改写旧轮次，暂停本会话采集以免覆盖；请新建会话。")
                if turn is None:
                    if source_turn is not current:
                        continue  # Do not opt old, previously unobserved history into collection.
                    db.execute("INSERT INTO turns(id,ordinal,started) VALUES(?,(SELECT COALESCE(MAX(ordinal),0)+1 FROM turns),?)",
                               (identity, source_turn["started"]))
                    turn = db.execute("SELECT * FROM turns WHERE id=?", (identity,)).fetchone()
                    db.execute("INSERT INTO copilot_source VALUES(?,?,?)", (identity, source_turn["number"], prompt_hash))

                def record(key, kind, event_data, started, ended, revision=None):
                    event_row(db, turn, key, kind, digest(event_data, revision), ended, max(0, ended - started))
                    db.execute("UPDATE events SET kind=? WHERE id=?", (kind, key))
                    entries.pop(key, None)
                    if event_data and (kind in {"user", "reply"} or mode == "full" or (mode == "tool-calls" and kind in {"tool", "tool_pending"})):
                        put_entry(entries, key, event_data)

                user = {"type": "message", "role": "user", "content": source_turn["prompt"]}
                record(digest(identity, "user"), "user", user, source_turn["started"], source_turn["started"])
                reply = source_turn["reply"]
                # Only the last no-tool assistant message of the current run is a final answer.
                if not source_turn["complete"]:
                    reply = None
                for item in source_turn["events"]:
                    kind, event_data = item["kind"], item["event"]
                    if item is reply:
                        kind = "reply"
                        event_data = {**event_data, "type": "message", "role": "assistant"}
                    key = digest(identity, item["key"])
                    prior = db.execute("SELECT kind FROM events WHERE id=?", (key,)).fetchone()
                    if kind == "tool_pending" and prior and prior["kind"] == "tool":
                        continue  # A PostToolUse payload may precede its source JSONL flush.
                    record(key, kind, event_data, item["started"], item["ended"], item.get("result"))
            write_jsonl(archive, db, entries, mode, session_id, AGENT)
            db.execute("INSERT OR REPLACE INTO copilot_watermark VALUES(1,?,?,?)",
                       (len(records), hook_stamp, records[0]["id"]))


def jsonc_object(text):
    """Parse JSONC while keeping exact offsets for narrow, comment-preserving edits."""
    token = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/')
    clean = token.sub(lambda match: re.sub(r"[^\r\n]", " ", match[0]) if match[0].startswith("/") else match[0], text)
    clean = re.sub(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])', lambda match: " " if match[0] == "," else match[0], clean)
    if clean.startswith("\ufeff"):
        clean = " " + clean[1:]

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate setting")
            result[key] = value
        return result

    try:
        config = json.loads(clean, object_pairs_hook=unique)
        if not isinstance(config, dict):
            raise ValueError("not an object")
        decoder = json.JSONDecoder()
        index = clean.index("{") + 1
        properties = {}
        while True:
            while clean[index].isspace() or clean[index] == ",":
                index += 1
            if clean[index] == "}":
                return config, properties, index, clean
            key, index = decoder.raw_decode(clean, index)
            while clean[index].isspace() or clean[index] == ":":
                index += 1
            start = index
            _value, index = decoder.raw_decode(clean, index)
            properties[key] = (start, index)
    except (ValueError, IndexError):
        raise TranscriptError("VS Code settings.json 不是有效的 JSONC 对象或含重复键；未覆盖原配置。") from None


def set_jsonc(text, keys, value, indent="  "):
    config, properties, close, _clean = jsonc_object(text)
    key = keys[0]
    if key in config:
        start, end = properties[key]
        if len(keys) > 1:
            if not isinstance(config[key], dict):
                raise TranscriptError("chat.hookFilesLocations 必须是对象，未覆盖原配置。")
            replacement = set_jsonc(text[start:end], keys[1:], value, indent + "  ")
        elif config[key] == value:
            return text
        else:
            replacement = json.dumps(value, ensure_ascii=False, indent=2)
        return text[:start] + replacement + text[end:]
    for nested in reversed(keys[1:]):
        value = {nested: value}
    encoded = json.dumps(value, ensure_ascii=False, indent=2).replace("\n", "\n" + indent)
    prefix = text[:close]
    if properties:
        end = list(properties.values())[-1][1]
        # clean masks trailing commas; consult the comment-masked original tail.
        tail = re.sub(r"//[^\r\n]*|/\*[\s\S]*?\*/", "", text[end:close])
        if "," not in tail:
            prefix = prefix[:end] + "," + prefix[end:]
    if not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + f'{indent}{json.dumps(key)}: {encoded}\n' + text[close:]


def hook_config(existing=None):
    config = existing if existing is not None else {"hooks": {}}
    if not isinstance(config, dict) or not isinstance(config.setdefault("hooks", {}), dict):
        raise TranscriptError("Copilot hooks 配置必须是 JSON 对象。")
    for event in EVENTS:
        entries = config["hooks"].setdefault(event, [])
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            raise TranscriptError("Copilot hook 命令列表格式错误。")
        config["hooks"][event] = [entry for entry in entries if entry.get("command") != COMMAND] + [
            {"type": "command", "command": COMMAND, "cwd": ".", "timeout": 10}]
    return config


def install_hooks(root):
    directory = root / ".vscode"
    runtime = directory / "rcore-hooks"
    settings = root / ".ai/ide/course.code-workspace"
    hooks = root / HOOK_LOCATION
    for path in (directory, runtime, root / ".ai", settings.parent, settings,
                 root / ".github", hooks.parent, hooks):
        if path.is_symlink():
            raise TranscriptError("VS Code 项目配置不能是符号链接，以免写入项目外部。")
    source = settings.read_text(encoding="utf-8") if settings.exists() else "{}\n"
    updated = set_jsonc(source, ["folders"], [{"name": root.name, "path": "../.."}])
    updated = set_jsonc(updated, ["settings", "chat.useHooks"], True)
    updated = set_jsonc(updated, ["settings", "chat.hookFilesLocations", ".github/hooks"], True)
    config = hook_config(json.loads(hooks.read_text(encoding="utf-8")) if hooks.exists() else None)
    # Keep configuration outside the chapter branches' tracked settings.json.
    # The standard hook location and installed runtime survive git switch.
    directory.mkdir(parents=True, exist_ok=True)
    settings.parent.mkdir(parents=True, exist_ok=True)
    hooks.parent.mkdir(parents=True, exist_ok=True)
    private_directory(runtime)
    for name in ("archive_session.py", "archive_storage.py", "copilot_hook.py"):
        atomic_write(runtime / name, (Path(__file__).resolve().parent / name).read_text(encoding="utf-8"))
    atomic_write(hooks, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    if updated != source or not settings.exists():
        atomic_write(settings, updated)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--install":
        try:
            install_hooks(Path(sys.argv[2]).resolve())
            return 0
        except TranscriptError as error:
            warn(str(error))
        except Exception as error:
            warn(f"安装失败（{type(error).__name__}），请检查项目配置与文件权限。")
        return 1
    try:
        handle(json.load(sys.stdin))
    except TranscriptError as error:
        warn(str(error))
    except Exception as error:
        warn(f"hook 未完成（{type(error).__name__}），请检查项目配置和文件权限；未更改 Agent 的权限决策。")
    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
