import json
from pathlib import Path


def records(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in strings(item)]
    return []


def archive_text(path):
    return "\n".join(strings(records(path)[1:]))


def event_count(path, kind):
    return sum(record.get("type") == kind for record in records(path)[1:])


def turn_count(path):
    return len({record["turn"] for record in records(path)[1:]})


def session_file(root, agent, session_id):
    directory = root / ".ai/agent-sessions" / agent
    files = list(directory.glob(f"*_{session_id}.jsonl"))
    if len(files) > 1:
        raise AssertionError("session created more than one archive")
    return files[0] if files else directory / f"2026-09-09_00-00-00_{session_id}.jsonl"
