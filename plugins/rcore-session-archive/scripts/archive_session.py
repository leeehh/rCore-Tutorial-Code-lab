#!/usr/bin/env python3
"""Archive a privacy-filtered coding-agent conversation as structured JSONL."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from itertools import chain
from pathlib import Path
from typing import Any, Dict, Iterator, Optional


ARCHIVE_DIR_NAME = ".ai/agent-sessions"
CONFIG_RELATIVE_PATHS = {
    "codex": Path(".codex") / "session-archive.json",
    "claude-code": Path(".claude") / "session-archive.json",
    "cursor": Path(".cursor") / "session-archive.json",
    "vscode-copilot": Path(".vscode") / "session-archive.json",
}
MODE_MESSAGES = "messages"
MODE_TOOL_CALLS = "tool-calls"
MODE_FULL = "full"
SUPPORTED_MODES = frozenset({MODE_MESSAGES, MODE_TOOL_CALLS, MODE_FULL})
SESSION_ID_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


def find_project_root(cwd: str, agent_name: str) -> Optional[Path]:
    """Use the nearest explicit project policy, including above chapter repos."""
    path = Path(cwd)
    if not path.is_absolute():
        return None
    path = path.resolve()
    for root in (path, *path.parents):
        config_path = root / CONFIG_RELATIVE_PATHS[agent_name]
        # A disabled or invalid nearer policy must never fall through to a parent.
        if config_path.exists() or config_path.is_symlink():
            return root
    return None


def safe_session_id(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = SESSION_ID_PATTERN.sub("_", value.strip()).strip("._")
    return normalized[:160] or None


def archive_session_id(value: Any) -> Optional[str]:
    normalized = safe_session_id(value)
    if normalized is None:
        return None
    if normalized != value:
        suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return normalized[:100] + "-" + suffix
    return normalized


def prepare_archive_directory(repository_root: Path, agent_name: str) -> Path:
    """Keep conversations under .ai without changing existing course-log permissions."""
    data_root = repository_root / ".ai"
    archive_root = repository_root / ARCHIVE_DIR_NAME
    directory = archive_root / agent_name
    for path in (data_root, archive_root, directory):
        if path.is_symlink():
            raise ValueError("refusing a symlink archive directory")
        path.mkdir(mode=0o700, exist_ok=True)
        if path != data_root:
            path.chmod(0o700)
    return directory


def read_archive_config(repository_root: Path, agent_name: str) -> Optional[Dict[str, Any]]:
    """Read only this agent's explicit, project-local opt-in on each callback."""
    config_path = repository_root / CONFIG_RELATIVE_PATHS[agent_name]
    if config_path.parent.is_symlink() or config_path.is_symlink():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        print(
            "rcore-session-archive: 无法读取项目归档配置，已跳过本次归档。",
            file=sys.stderr,
        )
        return None
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return None
    mode = config.get("mode", MODE_MESSAGES)
    if not isinstance(mode, str) or mode not in SUPPORTED_MODES:
        print(
            "rcore-session-archive: 归档等级无效，使用 messages。",
            file=sys.stderr,
        )
        mode = MODE_MESSAGES
    return {"enabled": True, "mode": mode}


def read_archive_mode(repository_root: Path, agent_name: str = "codex") -> str:
    config = read_archive_config(repository_root, agent_name)
    return config["mode"] if config else MODE_MESSAGES


def event_with_timestamp(record: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
    """Create a normalized archive event with optional source timestamp."""
    event: Dict[str, Any] = dict(fields)
    timestamp = record.get("timestamp")
    if isinstance(timestamp, str) and timestamp:
        event["timestamp"] = timestamp
    return event


def codex_tool_call(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return call metadata for a Codex response item, never its output."""
    item_type = payload.get("type")
    if not isinstance(item_type, str) or not item_type.endswith("_call"):
        return None

    event: Dict[str, Any] = {
        "type": "tool_call",
        "tool_type": item_type,
    }
    # Keep only request-side fields. In particular, do not copy fields named
    # output, result, error, or internal_chat_message_metadata_passthrough.
    for key in (
        "name",
        "call_id",
        "id",
        "server_label",
        "tool_name",
        "input",
        "arguments",
        "action",
        "command",
    ):
        if key in payload:
            event[key] = payload[key]
    return event


def filter_codex_record(
    record: Dict[str, Any], mode: str
) -> Iterator[Dict[str, Any]]:
    """Yield normalized user/final/call events from one Codex record."""
    if record.get("type") != "response_item":
        return
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return

    if payload.get("type") == "message":
        role = payload.get("role")
        content = payload.get("content")
        if role == "user":
            yield event_with_timestamp(
                record, type="message", role="user", content=content
            )
        elif role == "assistant" and payload.get("phase") == "final_answer":
            yield event_with_timestamp(
                record, type="message", role="assistant", content=content
            )
        elif mode == MODE_FULL and role in {"assistant", "system", "developer"}:
            yield event_with_timestamp(
                record, type="intermediate", role=role, content=content
            )
        return

    if mode in {MODE_TOOL_CALLS, MODE_FULL}:
        call = codex_tool_call(payload)
        if call is not None:
            yield event_with_timestamp(record, **call)
            return

    if mode == MODE_FULL:
        item_type = payload.get("type")
        if isinstance(item_type, str) and item_type.endswith("_call_output"):
            yield event_with_timestamp(
                record,
                type="tool_result",
                call_id=payload.get("call_id", payload.get("id")),
                content=payload.get("output", payload.get("content")),
            )
        elif item_type == "reasoning":
            # Opaque encrypted_content is not readable conversation text.
            content = [payload.get("summary"), payload.get("content")]
            yield event_with_timestamp(record, type="reasoning", content=content)


def claude_user_content(record: Dict[str, Any]) -> Optional[Any]:
    """Extract user-authored Claude content while rejecting tool results/meta."""
    if record.get("type") != "user":
        return None
    if record.get("isMeta") or record.get("isCompactSummary"):
        return None
    if record.get("isSidechain"):
        return None
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None

    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None

    user_blocks = [
        block
        for block in content
        if isinstance(block, dict)
        and block.get("type") in {"text", "image", "document"}
    ]
    return user_blocks or None


def claude_final_content(record: Dict[str, Any]) -> Optional[Any]:
    """Extract visible text from a completed Claude assistant turn."""
    if record.get("type") != "assistant" or record.get("isSidechain"):
        return None
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    if message.get("stop_reason") != "end_turn":
        return None

    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    text_blocks = [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return text_blocks or None


def filter_claude_record(
    record: Dict[str, Any], mode: str
) -> Iterator[Dict[str, Any]]:
    """Yield normalized user/final/call events from one Claude record."""
    user_content = claude_user_content(record)
    if user_content is not None:
        yield event_with_timestamp(
            record, type="message", role="user", content=user_content
        )

    final_content = claude_final_content(record)
    if final_content is not None and mode != MODE_FULL:
        yield event_with_timestamp(
            record, type="message", role="assistant", content=final_content
        )

    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        if (
            mode == MODE_FULL
            and record.get("type") == "assistant"
        ):
            yield event_with_timestamp(
                record, type="message" if final_content is not None else "intermediate",
                role="assistant", content=content
            )
        return

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if (
            block_type == "tool_use"
            and record.get("type") == "assistant"
            and mode in {MODE_TOOL_CALLS, MODE_FULL}
        ):
            call: Dict[str, Any] = {"type": "tool_call", "tool_type": "tool_use"}
            for source_key, destination_key in (
                ("name", "name"), ("id", "call_id"),
                ("input", "input"), ("caller", "caller"),
            ):
                if source_key in block:
                    call[destination_key] = block[source_key]
            yield event_with_timestamp(record, **call)
        elif mode == MODE_FULL:
            if block_type == "tool_result":
                yield event_with_timestamp(
                    record, type="tool_result", call_id=block.get("tool_use_id"),
                    content=block.get("content"), is_error=block.get("is_error", False),
                )
            elif block_type == "thinking":
                yield event_with_timestamp(
                    record, type="reasoning", content=block.get("thinking")
                )
            elif (
                block_type == "text"
                and record.get("type") == "assistant"
            ):
                yield event_with_timestamp(
                    record, type="message" if final_content is not None else "intermediate",
                    role="assistant", content=block.get("text")
                )


def read_jsonl_records(
    transcript_path: Path, allow_incomplete_tail: bool = False
) -> Iterator[Dict[str, Any]]:
    """Read a JSONL transcript one record at a time."""
    with transcript_path.open("rb") as input_file:
        for line_number, raw_line in enumerate(input_file, start=1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                # During Stop, Claude may still be appending the final JSONL
                # record. Its input text covers that reply until the next refresh.
                # Never conceal a malformed complete line or a mid-file error.
                if allow_incomplete_tail and not raw_line.endswith(b"\n"):
                    return
                raise ValueError(
                    f"invalid transcript JSON on line {line_number}: {error}"
                ) from error
            if not isinstance(record, dict):
                raise ValueError(
                    f"invalid transcript record on line {line_number}: "
                    "expected an object"
                )
            yield record


def is_main_claude_assistant(record: Dict[str, Any]) -> bool:
    message = record.get("message")
    return (
        record.get("type") == "assistant"
        and not record.get("isSidechain")
        and isinstance(message, dict)
        and message.get("role") == "assistant"
    )


def reconcile_claude_stop_message(
    pending: list[Dict[str, Any]], final_text: str
) -> Iterator[Dict[str, Any]]:
    """Merge Stop's authoritative final text with the last on-disk API message.

    Only the latest main-agent message since the last user/tool result is eligible
    for deduplication. An identical answer to an earlier prompt must be retained.
    Claude may persist the blocks of one API message as separate JSONL records.
    """
    assistants = [record for record in pending if is_main_claude_assistant(record)]
    text_parts = []
    text_records = []
    has_tool_call = False
    for record in assistants:
        content = record["message"].get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        for block in blocks if isinstance(blocks, list) else ():
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                has_tool_call = True
            text = block.get("text")
            if block.get("type") == "text" and isinstance(text, str) and text.strip():
                text_parts.append(text)
                text_records.append(record)

    # Block separators can differ between Claude versions. Normalize only line
    # endings and outer whitespace for matching; keep the original reply intact.
    expected = final_text.replace("\r\n", "\n").strip()
    candidates = {
        separator.join(text_parts).replace("\r\n", "\n").strip()
        for separator in ("", "\n", "\n\n")
    } - {""}
    if (
        not has_tool_call
        and expected in candidates
        and all(record["message"].get("stop_reason") == "end_turn" for record in text_records)
    ):
        yield from pending
        return

    replace_partial = not has_tool_call and any(expected.startswith(text) for text in candidates)
    for record in pending:
        if replace_partial and is_main_claude_assistant(record):
            message = record["message"]
            content = message.get("content")
            remaining = [
                block for block in content
                if not isinstance(block, dict) or block.get("type") != "text"
            ] if isinstance(content, list) else []
            # Preserve thinking and other non-text blocks for full archives.
            record = {**record, "message": {**message, "content": remaining}}
        yield record

    final_record: Dict[str, Any] = {
        "type": "assistant",
        "isSidechain": False,
        "message": {
            "role": "assistant", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": final_text}],
        },
    }
    if replace_partial and text_records and text_records[0].get("timestamp"):
        final_record["timestamp"] = text_records[0]["timestamp"]
    yield final_record


def claude_records_with_stop_message(
    records: Iterator[Dict[str, Any]], final_text: str
) -> Iterator[Dict[str, Any]]:
    """Buffer only the last API message, not the entire conversation or turn."""
    pending: list[Dict[str, Any]] = []
    pending_message_id = None
    for record in records:
        if is_main_claude_assistant(record):
            message_id = record["message"].get("id")
            if pending and (not message_id or message_id != pending_message_id):
                yield from pending
                pending = []
            pending_message_id = message_id
            pending.append(record)
        elif record.get("type") == "user" and not record.get("isSidechain"):
            # A new prompt (even the same text) or tool result starts a new
            # response boundary. Never deduplicate against the preceding answer.
            yield from pending
            pending = []
            pending_message_id = None
            yield record
        elif pending:
            pending.append(record)
        else:
            yield record
    yield from reconcile_claude_stop_message(pending, final_text)


def filtered_events(
    transcript_path: Path, agent_name: str, mode: str,
    last_assistant_message: Optional[str] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield privacy-filtered events for a supported coding agent."""
    filter_record = (
        filter_codex_record if agent_name == "codex" else filter_claude_record
    )
    include_stop_message = (
        agent_name == "claude-code"
        and isinstance(last_assistant_message, str)
        and bool(last_assistant_message.strip())
    )
    records = read_jsonl_records(transcript_path, allow_incomplete_tail=include_stop_message)
    if include_stop_message:
        records = claude_records_with_stop_message(records, last_assistant_message)
    for record in records:
        yield from filter_record(record, mode)


def sanitize_content(value: Any) -> Any:
    """Keep structured text and attachment references, never embedded binary data."""
    if isinstance(value, list):
        return [sanitize_content(item) for item in value]
    if isinstance(value, str) and value.startswith("data:"):
        return "[binary attachment omitted]"
    if not isinstance(value, dict):
        return value
    kind = value.get("type")
    if kind in {"image", "input_image", "image_url", "document", "input_audio", "audio"}:
        result = {"type": "attachment", "kind": kind}
        for key in ("title", "filename", "name", "path", "url", "image_url"):
            location = value.get(key)
            if isinstance(location, dict):
                location = location.get("url")
            if isinstance(location, str) and not location.startswith("data:"):
                result[key] = location
        source = value.get("source")
        if isinstance(source, dict):
            if isinstance(source.get("url"), str) and not source["url"].startswith("data:"):
                result["url"] = source["url"]
            if kind == "document" and source.get("type") == "text":
                result["text"] = source.get("data", "")
        return result
    return {key: sanitize_content(item) for key, item in value.items()
            if key not in {"encrypted_content", "signature"}}


def utc_timestamp(value: Any = None) -> str:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
        if stamp is None or stamp.tzinfo is None:
            raise ValueError("missing timezone")
    except (ValueError, OverflowError):
        stamp = datetime.now(timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat(timespec="seconds")


def existing_archive(directory: Path, session_id: str) -> Optional[Path]:
    pattern = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_" + re.escape(session_id) + r"\.jsonl$")
    matches = [path for path in directory.glob(f"*_{session_id}.jsonl") if pattern.fullmatch(path.name)]
    if len(matches) > 1:
        raise ValueError("multiple archives for one session; preserve existing files")
    return matches[0] if matches else None


def session_archive_path(directory: Path, session_id: str, started_at: str) -> Path:
    existing = existing_archive(directory, session_id)
    if existing is not None:
        return existing
    stamp = datetime.fromisoformat(utc_timestamp(started_at))
    return directory / f"{stamp:%Y-%m-%d_%H-%M-%S}_{session_id}.jsonl"


def session_header(agent_name: str, session_id: str, mode: str, started_at: str) -> Dict[str, Any]:
    return {"type": "session", "schema_version": 1, "agent": agent_name,
            "session_id": session_id, "started_at": utc_timestamp(started_at), "mode": mode}


def write_archive(
    transcript_path: Path,
    output_file: Any,
    agent_name: str,
    mode: str,
    session_id: Optional[str] = None,
    last_assistant_message: Optional[str] = None,
    started_at: Optional[str] = None,
) -> None:
    """Stream a session header and mode-filtered events, one JSON object per line."""
    events = filtered_events(transcript_path, agent_name, mode, last_assistant_message)
    first_event = next(events, None)
    started = started_at or (first_event or {}).get("timestamp")

    def write(value):
        output_file.write((json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))

    write(session_header(agent_name, session_id or transcript_path.stem, mode, started))
    turn = 0
    agent_has_responded = False
    for event in chain([first_event], events) if first_event else ():
        is_user = event.get("type") == "message" and event.get("role") == "user"
        is_context = event.get("role") in {"system", "developer"}
        if not is_context and (turn == 0 or (is_user and agent_has_responded)):
            turn += 1
            agent_has_responded = False
        write({**sanitize_content(event), "turn": turn})
        if not is_user and not is_context:
            agent_has_responded = True


def archive_transcript(payload: Dict[str, Any]) -> None:
    transcript_value = payload.get("transcript_path")
    raw_session_id = payload.get("session_id")
    session_id = archive_session_id(raw_session_id)
    cwd = payload.get("cwd")
    if not isinstance(transcript_value, str) or not transcript_value or not session_id:
        return
    if not isinstance(cwd, str) or not cwd:
        return
    agent_name = "codex" if os.environ.get("PLUGIN_ROOT") else "claude-code"
    repository_root = find_project_root(cwd, agent_name)
    if repository_root is None:
        return
    config = read_archive_config(repository_root, agent_name)
    if config is None:
        return
    transcript_path = Path(transcript_value).expanduser()
    try:
        transcript_path = transcript_path.resolve(strict=True)
    except OSError:
        return
    if not transcript_path.is_file():
        return
    last_assistant_message = (payload.get("last_assistant_message")
                              if agent_name == "claude-code" and payload.get("hook_event_name") == "Stop" else None)
    archive_dir = prepare_archive_directory(repository_root, agent_name)
    records = read_jsonl_records(transcript_path, allow_incomplete_tail=(
        isinstance(last_assistant_message, str) and bool(last_assistant_message.strip())
    ))
    try:
        first_record = next(records, {})
    finally:
        records.close()
    started_at = utc_timestamp(first_record.get("timestamp"))
    destination = session_archive_path(archive_dir, session_id, started_at)
    if destination.is_symlink() or destination.resolve() == transcript_path:
        raise ValueError("refusing to replace a symlink or the source transcript")
    if destination.exists():
        # Keep the first archival timestamp even if source history is regenerated.
        with destination.open(encoding="utf-8") as source:
            header = json.loads(source.readline())
        if not isinstance(header, dict) or header.get("type") != "session" or header.get("session_id") != raw_session_id:
            raise ValueError("existing session archive has an invalid header")
        started_at = header["started_at"]
    descriptor, temporary_name = tempfile.mkstemp(dir=archive_dir, prefix=f".{session_id}.", suffix=".tmp")
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            write_archive(transcript_path, output, agent_name, config["mode"], raw_session_id,
                          last_assistant_message, started_at)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook input must be a JSON object")
        archive_transcript(payload)
    except Exception as error:
        print(f"rcore-session-archive: hook 未完成（{type(error).__name__}），请检查配置与源会话文件。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
