"""Verify opt-in scope and the actual transcript hook without an agent process."""

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from archive_test_helpers import session_file, records
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import archive_session as archive
import cursor_hook as cursor
import copilot_hook as copilot


class ProjectArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "renamed tutorial"
        self.root.mkdir()
        self.source = self.root.parent / "source.jsonl"
        self.payload = {"cwd": str(self.root), "session_id": "session", "transcript_path": str(self.source),
                        "hook_event_name": "Stop", "last_assistant_message": "FINAL_REPLY"}
        self.records = [{"type": "user", "message": {"role": "user", "content": "USER_INPUT"}}]
        self.flush()

    def flush(self):
        self.source.write_text("".join(json.dumps(record) + "\n" for record in self.records))

    def config(self, agent, value=None, root=None):
        path = (root or self.root) / archive.CONFIG_RELATIVE_PATHS[agent]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value if value is not None else {"enabled": True, "mode": "messages"}))
        return path

    def invoke(self, agent="claude-code"):
        environment = {"PLUGIN_ROOT": "plugin"} if agent == "codex" else {"CLAUDE_PLUGIN_ROOT": "plugin"}
        with mock.patch.dict(os.environ, environment, clear=True):
            archive.archive_transcript(self.payload)
        return session_file(self.root, agent, "session")

    def test_each_agent_needs_its_own_explicit_enable(self):
        self.config("codex", {"enabled": True, "mode": "full"})
        for value in (None, {}, {"enabled": False}, {"enabled": "true"}, []):
            path = self.root / archive.CONFIG_RELATIVE_PATHS["claude-code"]
            if value is None:
                path.unlink(missing_ok=True)
            else:
                self.config("claude-code", value)
            self.assertFalse(self.invoke().exists())
        self.config("claude-code")
        self.assertIn("FINAL_REPLY", self.invoke().read_text())

    def test_invalid_json_disables_and_invalid_mode_falls_back(self):
        path = self.config("claude-code")
        path.write_text('{"enabled":true,')
        with redirect_stderr(io.StringIO()):
            self.assertFalse(self.invoke().exists())
        for mode in (None, [], "invalid"):
            self.config("claude-code", {"enabled": True, "mode": mode})
            with redirect_stderr(io.StringIO()):
                self.assertEqual("messages", archive.read_archive_mode(self.root, "claude-code"))

    def test_nearest_policy_covers_nested_chapter_repository(self):
        self.config("claude-code")
        chapter = self.root / "chapter-3"
        (chapter / ".git").mkdir(parents=True)
        self.payload["cwd"] = str(chapter)
        self.assertEqual(self.root, archive.find_project_root(str(chapter), "claude-code"))
        path = self.invoke()
        original = path.read_bytes()
        child_config = self.config("claude-code", {"enabled": False}, chapter)
        self.assertEqual(chapter, archive.find_project_root(str(chapter), "claude-code"))
        self.payload["last_assistant_message"] = "DISABLED_REPLY"
        self.invoke()
        self.assertEqual(original, path.read_bytes())
        child_config.write_text("broken")
        with redirect_stderr(io.StringIO()):
            self.invoke()
        self.assertEqual(original, path.read_bytes())

    def test_unconfigured_sibling_project_is_not_archived(self):
        self.config("claude-code")
        other = self.root.parent / "other"
        other.mkdir()
        self.payload["cwd"] = str(other)
        self.assertFalse(self.invoke().exists())
        self.assertFalse((other / ".ai/agent-sessions").exists())

    def test_stop_then_session_end_keep_one_reply_and_source_unchanged(self):
        self.config("claude-code")
        before = self.source.read_bytes()
        path = self.invoke()
        self.invoke()
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual(1, path.read_text().count("FINAL_REPLY"))
        self.records.append({"type": "assistant", "message": {"role": "assistant", "stop_reason": "end_turn", "content": "FINAL_REPLY"}})
        self.flush()
        self.payload.update(hook_event_name="SessionEnd", last_assistant_message="STALE_REPLY")
        self.invoke()
        self.assertEqual(1, path.read_text().count("FINAL_REPLY"))
        self.assertNotIn("STALE_REPLY", path.read_text())
        self.assertEqual([path], list(path.parent.glob("*.jsonl")))
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual(0o700, path.parent.stat().st_mode & 0o777)

    def test_modes_refresh_same_codex_archive_and_preserve_source(self):
        self.records = [
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "USER_INPUT"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "call", "arguments": {"command": "COMMAND_ONLY"}}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call", "output": "TOOL_OUTPUT"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": "CODEX_FINAL"}},
        ]
        self.flush()
        before = self.source.read_bytes()
        for mode in ("full", "tool-calls", "messages"):
            self.config("codex", {"enabled": True, "mode": mode})
            text = self.invoke("codex").read_text()
            self.assertIn("CODEX_FINAL", text)
            self.assertEqual(mode != "messages", "COMMAND_ONLY" in text)
            self.assertEqual(mode == "full", "TOOL_OUTPUT" in text)
        self.assertEqual(before, self.source.read_bytes())

    def test_failed_refresh_preserves_existing_archive_and_other_sessions(self):
        self.config("claude-code")
        path = self.invoke()
        before = path.read_bytes()
        legacy = path.parent / "other.jsonl"
        legacy.write_text("KEEP")
        self.source.write_text("broken\n")
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual(before, path.read_bytes())
        self.assertEqual("KEEP", legacy.read_text())
        self.assertEqual([], list(path.parent.glob("*.tmp")))
        self.flush()
        self.invoke()
        self.assertTrue(legacy.exists())
        self.assertTrue(self.source.exists())

    def test_config_and_archive_symlinks_do_not_escape_project(self):
        config = self.config("claude-code")
        moved = config.with_name("external.json")
        config.rename(moved)
        config.symlink_to(moved)
        self.assertFalse(self.invoke().exists())
        config.unlink()
        self.config("claude-code")
        outside = self.root.parent / "outside"
        outside.mkdir()
        (self.root / ".ai").mkdir()
        (self.root / ".ai/agent-sessions").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual([], list(outside.iterdir()))

    def test_unsafe_session_ids_do_not_collide_or_escape(self):
        self.assertNotEqual(archive.archive_session_id("a/b"), archive.archive_session_id("a?b"))
        self.assertIsNone(archive.archive_session_id(".."))
        self.assertNotIn("/", archive.archive_session_id("../a"))

    def test_filename_uses_utc_date_and_stays_stable_when_source_changes(self):
        self.config("claude-code")
        self.records[0]["timestamp"] = "2026-09-09T20:34:56+08:00"
        self.flush()
        path = self.invoke()
        self.assertEqual("2026-09-09_12-34-56_session.jsonl", path.name)
        self.records[0]["timestamp"] = "2026-09-10T20:34:56+08:00"
        self.flush()
        self.assertEqual(path, self.invoke())
        self.assertEqual("2026-09-09T12:34:56+00:00", records(path)[0]["started_at"])
        self.assertEqual([path], list(path.parent.glob("*.jsonl")))

    def test_stop_reply_recovers_even_when_the_first_source_line_is_incomplete(self):
        self.config("claude-code")
        self.source.write_bytes(b'{"partial":')
        path = self.invoke()
        self.assertIn("FINAL_REPLY", path.read_text())
        self.assertEqual(b'{"partial":', self.source.read_bytes())

    def test_existing_ai_directory_permissions_are_preserved(self):
        self.config("claude-code")
        directory = self.root / ".ai"
        directory.mkdir(mode=0o755)
        self.invoke()
        self.assertEqual(0o755, directory.stat().st_mode & 0o777)

    def test_real_cli_uses_project_config_and_never_blocks_on_bad_json(self):
        self.config("claude-code")
        environment = {key: value for key, value in os.environ.items() if key != "PLUGIN_ROOT"}
        environment["CLAUDE_PLUGIN_ROOT"] = "plugin"
        for payload in (json.dumps(self.payload), '{"PRIVATE_CONTENT":'):
            result = subprocess.run([sys.executable, str(SCRIPTS / "archive_session.py")], input=payload,
                                    text=True, capture_output=True, env=environment, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn("PRIVATE_CONTENT", result.stderr)
        self.assertIn("FINAL_REPLY", session_file(self.root, "claude-code", "session").read_text())

    def test_all_adapters_work_with_socket_connections_forbidden(self):
        for agent in archive.CONFIG_RELATIVE_PATHS:
            self.config(agent)
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")) as connect:
            self.invoke()
            self.invoke("codex")
            cursor.handle({"hook_event_name": "beforeSubmitPrompt", "workspace_roots": [str(self.root)],
                           "conversation_id": "offline", "generation_id": "one", "prompt": "OFFLINE_INPUT"}, self.root)
            copilot_source = self.root.parent / "copilot.jsonl"
            stamp = "2026-09-09T12:00:00Z"
            records = [
                {"id": "header", "type": "session.start", "timestamp": stamp, "data": {"version": 1, "sessionId": "offline"}},
                {"id": "user", "type": "user.message", "timestamp": stamp, "data": {"content": "OFFLINE_INPUT"}},
            ]
            copilot_source.write_text("".join(json.dumps(item) + "\n" for item in records))
            copilot.handle({"hook_event_name": "UserPromptSubmit", "prompt": "OFFLINE_INPUT", "session_id": "offline",
                            "cwd": str(self.root), "timestamp": stamp, "transcript_path": str(copilot_source)}, self.root)
            connect.assert_not_called()
        self.assertEqual(4, len(list((self.root / ".ai/agent-sessions").rglob("*.jsonl"))))


if __name__ == "__main__":
    unittest.main()
