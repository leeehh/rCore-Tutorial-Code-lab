"""Local JSONL storage and a content-free ordering index for event adapters."""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from archive_session import sanitize_content, session_header


def digest(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def atomic_write(path, text):
    if path.is_symlink():
        raise ValueError("refusing a symlink output")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def private_directory(path):
    if path.is_symlink():
        raise ValueError("refusing a symlink archive directory")
    path.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)


def read_entries(path, agent_name, session_id):
    if path.is_symlink():
        raise ValueError("refusing a symlink archive")
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as source:
        header = json.loads(source.readline())
        if not isinstance(header, dict) or header.get("type") != "session" or header.get("schema_version") != 1:
            raise ValueError("existing archive has an invalid header")
        if header.get("agent") != agent_name or header.get("session_id") != session_id:
            raise ValueError("existing archive belongs to a different session")
        entries = {}
        for line in source:
            if not line.strip():
                continue
            entry = json.loads(line)
            key = entry.get("event_id") if isinstance(entry, dict) else None
            if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key) or key in entries:
                raise ValueError("existing archive has an invalid or duplicate event ID")
            entries[key] = entry
        return entries


def put_entry(entries, key, event):
    entries[key] = sanitize_content(event)


def open_index(path):
    if path.is_symlink():
        raise ValueError("refusing a symlink index")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    path.chmod(0o600)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS turns (
            id TEXT PRIMARY KEY, ordinal INTEGER UNIQUE, started INTEGER,
            phase INTEGER DEFAULT 0, reply TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS events (
            ordinal INTEGER PRIMARY KEY, id TEXT UNIQUE, turn_id TEXT,
            kind TEXT, started INTEGER, ended INTEGER, content_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS text_callbacks (
            turn_id TEXT, event TEXT, phase INTEGER, content_hash TEXT, event_id TEXT,
            PRIMARY KEY(turn_id,event,phase)
        );
    """)
    return db


def text_event_id(db, turn, event, text):
    # Text callbacks have no tool_use_id. Only coalesce consecutive identical
    # notifications in the same tool phase; A -> B -> A are three real messages.
    fingerprint = digest(text)
    previous = db.execute(
        "SELECT * FROM text_callbacks WHERE turn_id=? AND event=? AND phase=?",
        (turn["id"], event, turn["phase"]),
    ).fetchone()
    if previous and previous["content_hash"] == fingerprint:
        return previous["event_id"]
    ordinal = db.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM events").fetchone()[0]
    key = digest(turn["id"], event, turn["phase"], ordinal, fingerprint)
    db.execute("INSERT OR REPLACE INTO text_callbacks VALUES(?,?,?,?,?)",
               (turn["id"], event, turn["phase"], fingerprint, key))
    return key


def event_row(db, turn, key, kind, fingerprint, now, duration=0):
    row = db.execute("SELECT * FROM events WHERE id=?", (key,)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO events(id,turn_id,kind,started,ended,content_hash) VALUES(?,?,?,?,?,?)",
            (key, turn["id"], kind, max(turn["started"], now - duration), now, fingerprint),
        )
    elif row["content_hash"] != fingerprint:
        db.execute("UPDATE events SET ended=?,content_hash=?,kind=? WHERE id=?",
                   (now, fingerprint, kind, key))
    return db.execute("SELECT * FROM events WHERE id=?", (key,)).fetchone()


def write_jsonl(path, db, entries, mode, session_id, agent_name):
    turns = db.execute("SELECT * FROM turns ORDER BY ordinal").fetchall()
    if not turns:
        return
    started = datetime.fromtimestamp(turns[0]["started"] / 1_000_000_000, timezone.utc).isoformat(timespec="seconds")
    records = [session_header(agent_name, session_id, mode, started)]
    for turn in turns:
        for row in db.execute("SELECT * FROM events WHERE turn_id=? ORDER BY ordinal", (turn["id"],)):
            key, kind = row["id"], row["kind"]
            keep = kind in {"user", "reply"} or mode == "full" or (mode == "tool-calls" and kind in {"tool", "tool_pending"})
            if not keep:
                entries.pop(key, None)
            body = entries.get(key)
            if body:
                stamp = datetime.fromtimestamp(row["started"] / 1_000_000_000, timezone.utc).isoformat(timespec="seconds")
                records.append({**body, "event_id": key, "turn": turn["ordinal"], "timestamp": body.get("timestamp", stamp)})
    if len(records) > 1 or path.exists():
        text = "".join(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n" for record in records)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            atomic_write(path, text)
