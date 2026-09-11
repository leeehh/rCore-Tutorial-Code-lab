"""Local archive regressions adapted from rCore-Tutorial-Code-2025S."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from archive_test_helpers import records, archive_text, event_count, turn_count, session_file
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
REPOSITORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SCRIPTS))
import copilot_hook as copilot

class CopilotHookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_path = self.root / ".vscode/session-archive.json"
        self.config_path.parent.mkdir()
        self.config = {"enabled": True, "mode": "messages"}
        self.save_config()
        self.source = self.root / "copilot-source.jsonl"
        self.records = []
        self.add("session.start", {"sessionId": "session", "version": 1, "producer": "copilot-agent", "context": {"cwd": str(self.root)}})
        patcher = mock.patch.object(copilot.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)


    @property
    def archive(self):
        return session_file(self.root, "vscode-copilot", "session")


    def save_config(self, mode=None):
        if mode:
            self.config["mode"] = mode
        self.config_path.write_text(json.dumps(self.config))


    def add(self, kind, data):
        index = len(self.records)
        self.records.append({"id": f"record-{index}", "parentId": f"record-{index - 1}" if index else None,
                             "timestamp": (datetime(2026, 9, 6, 12, tzinfo=timezone.utc) + timedelta(seconds=index)).isoformat(), "type": kind, "data": data})
        self.flush()


    def flush(self):
        self.source.write_text("".join(json.dumps(record) + "\n" for record in self.records))


    def message(self, text, requests=None, reasoning=None):
        data = {"messageId": f"message-{len(self.records)}", "content": text, "toolRequests": requests or []}
        if reasoning:
            data["reasoningText"] = reasoning
        self.add("assistant.message", data)


    def event(self, name="Stop", **fields):
        payload = {"hook_event_name": name, "session_id": "session", "transcript_path": str(self.source),
                   "cwd": str(self.root), "timestamp": "2026-09-06T13:00:00+00:00", **fields}
        copilot.handle(payload, self.root)
        return payload


    def add_turn(self, prompt="USER_INPUT", reply="FINAL_REPLY", tool=True):
        self.add("user.message", {"content": prompt, "attachments": []})
        self.add("assistant.turn_start", {"turnId": "0"})
        if tool:
            self.message("INTERMEDIATE_TEXT", [{"toolCallId": "call-1", "name": "run_in_terminal", "arguments": '{"command":"printf COMMAND_TEXT"}', "type": "function"}], "THINKING_TEXT")
            self.add("tool.execution_start", {"toolCallId": "call-1", "toolName": "run_in_terminal", "arguments": {"command": "printf COMMAND_TEXT"}})
            self.add("tool.execution_complete", {"toolCallId": "call-1", "success": True, "result": {"content": "OUTPUT_TEXT\nnext line"}})
            self.add("assistant.turn_end", {"turnId": "0"})
            self.add("assistant.turn_start", {"turnId": "1"})
        self.message(reply)
        self.add("assistant.turn_end", {"turnId": "1" if tool else "0"})


    def test_messages_mode_saves_only_user_and_final_content(self):
        self.add_turn()
        original = self.source.read_bytes()
        self.event()
        text = archive_text(self.archive)
        self.assertIn("USER_INPUT", text)
        self.assertIn("FINAL_REPLY", text)
        for value in ("INTERMEDIATE_TEXT", "THINKING_TEXT", "COMMAND_TEXT", "OUTPUT_TEXT"):
            for path in (self.root / ".ai/agent-sessions").rglob("*"):
                if path.is_file():
                    self.assertNotIn(value.encode(), path.read_bytes(), str(path))
        self.assertEqual([self.archive], list((self.root / ".ai/agent-sessions").rglob("*.jsonl")))
        self.assertEqual([], list((self.root / ".ai/agent-sessions").rglob("*.md")))
        self.assertEqual(original, self.source.read_bytes())


    def test_tool_calls_and_full_modes(self):
        self.add_turn()
        self.save_config("tool-calls")
        self.event()
        text = archive_text(self.archive)
        self.assertEqual({"command": "printf COMMAND_TEXT"}, next(item for item in records(self.archive) if item["type"] == "tool_call")["input"])
        self.assertEqual(1, event_count(self.archive, "tool_call"))
        self.assertNotIn("OUTPUT_TEXT", text)
        self.assertNotIn("INTERMEDIATE_TEXT", text)
        self.save_config("full")
        self.event()
        text = archive_text(self.archive)
        for value in ("INTERMEDIATE_TEXT", "THINKING_TEXT", "COMMAND_TEXT", "OUTPUT_TEXT"):
            self.assertIn(value, text)
        self.assertEqual(1, event_count(self.archive, "tool_call"))
        self.assertEqual(1, event_count(self.archive, "tool_result"))
        self.save_config("messages")
        self.event()
        self.assertNotIn("OUTPUT_TEXT", archive_text(self.archive))
        self.assertNotIn("COMMAND_TEXT", archive_text(self.archive))


    def test_stop_and_duplicate_transcript_entries_keep_one_copy(self):
        self.add_turn()
        self.event()
        first = self.archive.read_bytes()
        self.event()
        self.records.append(self.records[-1])
        self.flush()
        self.event()
        self.assertEqual(first, self.archive.read_bytes())

    def test_date_filename_is_stable_across_turns(self):
        self.add_turn()
        self.event()
        path = self.archive
        self.assertEqual("2026-09-06_12-00-01_session.jsonl", path.name)
        self.add_turn("another question", "another answer", tool=False)
        self.event()
        self.assertEqual(path, self.archive)
        self.assertEqual(2, turn_count(path))


    def test_multiple_llm_rounds_are_one_user_turn_and_identical_user_turns_stay_distinct(self):
        self.add_turn()
        self.event()
        self.add_turn()
        self.event()
        text = archive_text(self.archive)
        self.assertEqual(2, text.count("USER_INPUT"))
        self.assertEqual(2, text.count("FINAL_REPLY"))
        self.assertEqual(2, turn_count(self.archive))


    def test_recreated_history_with_new_uuids_does_not_duplicate(self):
        self.add_turn()
        self.event()
        before = self.archive.read_bytes()
        for index, record in enumerate(self.records):
            record["id"] = f"replayed-{index}"
            record["parentId"] = f"replayed-{index-1}" if index else None
            if record["type"] == "assistant.message":
                record["data"]["messageId"] = f"new-message-id-{index}"
        self.flush()
        self.event()
        self.assertEqual(before, self.archive.read_bytes())


    def test_official_history_replay_without_tool_results_retains_archive_and_accepts_new_turn(self):
        self.save_config("full")
        self.add_turn()
        self.event()
        original = self.archive.read_bytes()
        # Official _replayHistory emits assistant rounds and toolRequests, not
        # tool.execution_* entries. Every entry is assigned a new UUID.
        self.records = [record for record in self.records if not record["type"].startswith("tool.execution_")]
        for index, record in enumerate(self.records):
            record["id"] = f"history-{index}"
            record["parentId"] = f"history-{index - 1}" if index else None
            if record["type"] in {"assistant.turn_start", "assistant.turn_end"}:
                record["data"]["turnId"] = "0." + record["data"]["turnId"]
        self.flush()
        self.event(timestamp="2026-09-06T14:00:00Z")
        self.assertEqual(original, self.archive.read_bytes())
        self.add_turn("NEW_PROMPT", "NEW_REPLY", tool=False)
        self.event(timestamp="2026-09-06T15:00:00Z")
        self.assertIn("OUTPUT_TEXT", archive_text(self.archive))
        self.assertIn("NEW_REPLY", archive_text(self.archive))
        self.assertEqual(2, turn_count(self.archive))


    def test_enabling_on_an_existing_conversation_does_not_import_old_turns(self):
        self.add_turn("OLD_PRIVATE_PROMPT", "OLD_PRIVATE_REPLY", tool=False)
        self.add_turn("NEW_PROMPT", "NEW_REPLY", tool=False)
        self.event()
        text = archive_text(self.archive)
        self.assertNotIn("OLD_PRIVATE", text)
        self.assertIn("NEW_REPLY", text)
        self.add_turn("THIRD_PROMPT", "THIRD_REPLY", tool=False)
        self.event()
        self.assertNotIn("OLD_PRIVATE", archive_text(self.archive))
        self.assertEqual(2, turn_count(self.archive))


    def test_session_start_and_empty_stop_create_no_archive(self):
        self.event("SessionStart")
        self.event()
        self.assertFalse((self.root / ".ai/agent-sessions").exists())


    def test_user_prompt_must_be_the_current_transcript_prompt(self):
        self.add_turn("old", "old reply", tool=False)
        with self.assertRaisesRegex(copilot.TranscriptError, "最新用户输入"):
            self.event("UserPromptSubmit", prompt="not flushed new prompt")
        self.assertFalse(self.archive.exists())


    def test_hook_tool_result_fills_a_not_yet_flushed_transcript(self):
        self.save_config("full")
        self.add("user.message", {"content": "question"})
        self.add("assistant.turn_start", {"turnId": "0"})
        self.message("running", [{"toolCallId": "call", "name": "run_in_terminal", "arguments": '{"command":"pwd"}', "type": "function"}])
        self.event("PreToolUse", tool_name="run_in_terminal", tool_use_id="call__vscode-3", tool_input={"command": "pwd"})
        self.event("PostToolUse", tool_name="run_in_terminal", tool_use_id="call__vscode-3", tool_input={"command": "pwd"}, tool_response="RESULT_FROM_HOOK")
        self.assertIn("RESULT_FROM_HOOK", archive_text(self.archive))
        self.add("tool.execution_start", {"toolCallId": "call", "toolName": "run_in_terminal", "arguments": {"command": "pwd"}})
        self.add("tool.execution_complete", {"toolCallId": "call", "success": True, "result": {"content": "RESULT_FROM_HOOK"}})
        self.add("assistant.turn_end", {"turnId": "0"})
        self.event()
        self.assertEqual(1, archive_text(self.archive).count("RESULT_FROM_HOOK"))
        self.assertEqual(1, event_count(self.archive, "tool_call"))


    def test_missing_final_reply_does_not_use_intermediate_text_as_final(self):
        self.add_turn()
        self.records = self.records[:7]
        self.flush()
        self.event()
        self.assertNotIn("INTERMEDIATE_TEXT", archive_text(self.archive))


    def test_delayed_final_flush_is_retried(self):
        self.add("user.message", {"content": "question"})
        self.add("assistant.turn_start", {"turnId": "0"})
        def flush_later(_delay):
            self.message("DELAYED_FINAL")
            self.add("assistant.turn_end", {"turnId": "0"})
        self.sleep.side_effect = flush_later
        self.event()
        self.assertIn("DELAYED_FINAL", archive_text(self.archive))
        self.assertEqual(1, self.sleep.call_count)


    def test_corrupt_or_unsupported_source_does_not_overwrite_archive(self):
        self.add_turn()
        self.event()
        original = self.archive.read_bytes()
        source = self.source.read_bytes()
        variants = [source + b'{"partial":', b'not-json\n', source.replace(b'"version": 1', b'"version": 2'), source.replace(b'"sessionId": "session"', b'"sessionId": "another-session"')]
        for content in variants:
            self.source.write_bytes(content)
            with self.assertRaises(copilot.TranscriptError):
                self.event()
            self.assertEqual(original, self.archive.read_bytes())


    def test_stale_hook_cannot_roll_back_newer_jsonl(self):
        self.add_turn()
        self.event(timestamp="2026-09-06T14:00:00Z")
        original = self.archive.read_bytes()
        self.records = self.records[:7]
        self.flush()
        self.event(timestamp="2026-09-06T13:00:00Z")
        self.assertEqual(original, self.archive.read_bytes())


    def test_newer_hook_with_truncated_source_reports_error_without_overwrite(self):
        self.add_turn()
        self.event()
        original = self.archive.read_bytes()
        self.records = self.records[:7]
        self.flush()
        with self.assertRaisesRegex(copilot.TranscriptError, "截断"):
            self.event(timestamp="2026-09-06T14:00:00Z")
        self.assertEqual(original, self.archive.read_bytes())


    def test_changed_user_order_is_detected_instead_of_overwriting_a_turn(self):
        self.add_turn()
        self.event()
        original = self.archive.read_bytes()
        self.records[1]["data"]["content"] = "changed earlier prompt"
        self.flush()
        with self.assertRaisesRegex(copilot.TranscriptError, "重排"):
            self.event()
        self.assertEqual(original, self.archive.read_bytes())


    def test_scope_disabled_and_other_agent_configs_do_not_activate(self):
        self.add_turn()
        self.event(cwd="/other-project")
        self.event(cwd="relative-project")
        self.config["enabled"] = False
        self.save_config()
        self.event()
        self.config_path.unlink()
        (self.root / ".cursor").mkdir()
        (self.root / ".cursor/session-archive.json").write_text('{"enabled":true,"mode":"full"}')
        self.event()
        self.assertFalse(self.archive.exists())


    def test_concurrent_identical_callbacks_keep_one_copy(self):
        self.add_turn()
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(lambda _: self.event(), range(8)))
        self.assertEqual(1, archive_text(self.archive).count("FINAL_REPLY"))


    def test_real_cli_fails_open_without_printing_source_content(self):
        result = subprocess.run([sys.executable, str(SCRIPTS / "copilot_hook.py")], input='{"secret":"DO_NOT_LOG",', text=True, capture_output=True)
        self.assertEqual(0, result.returncode)
        self.assertEqual({}, json.loads(result.stdout))
        self.assertNotIn("DO_NOT_LOG", result.stderr)


class CopilotSetupTests(unittest.TestCase):
    def test_jsonc_edits_preserve_comments_settings_strings_and_are_idempotent(self):
        source = '''{
  // keep this editor preference
  "editor.fontSize": 16,
  "example": "text,} https://example.test/*not a comment*/",
  "chat.useHooks": false, // local override
  "chat.hookFilesLocations": {
    "custom/hooks": true, /* retain this */
  },
}
'''
        updated = copilot.set_jsonc(source, ["chat.useHooks"], True)
        updated = copilot.set_jsonc(updated, ["chat.hookFilesLocations", copilot.HOOK_LOCATION], True)
        config = copilot.jsonc_object(updated)[0]
        self.assertTrue(config["chat.useHooks"])
        self.assertTrue(config["chat.hookFilesLocations"][copilot.HOOK_LOCATION])
        self.assertTrue(config["chat.hookFilesLocations"]["custom/hooks"])
        self.assertEqual(16, config["editor.fontSize"])
        for value in ("// keep this editor preference", "// local override", "/* retain this */", '"example": "text,} https://example.test/*not a comment*/"'):
            self.assertIn(value, updated)
        self.assertEqual(updated, copilot.set_jsonc(updated, ["chat.hookFilesLocations", copilot.HOOK_LOCATION], True))


    def test_jsonc_insertion_handles_empty_comments_bom_and_no_trailing_comma(self):
        for source in ('{}', '{/* only comment */}', '\ufeff{\n"other":42 // end comment\n}', '{"other": {"inner":[1,2,],},}'):
            with self.subTest(source=source):
                updated = copilot.set_jsonc(source, ["chat.useHooks"], True)
                self.assertTrue(copilot.jsonc_object(updated)[0]["chat.useHooks"])


    def test_invalid_config_is_not_replaced_and_installer_reports_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".ai/ide").mkdir(parents=True)
            path = root / ".ai/ide/course.code-workspace"
            for source in ('{"broken":', '{"duplicate":1,"duplicate":2}', '[]', '{"settings":{"chat.hookFilesLocations": false}}'):
                path.write_text(source)
                result = subprocess.run([sys.executable, str(SCRIPTS / "copilot_hook.py"), "--install", str(root)], text=True, capture_output=True)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(source, path.read_text())
                self.assertFalse((root / ".vscode/rcore-hooks").exists())


    def test_installer_matches_template_and_preserves_unrelated_hooks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            chapter_settings = root / ".vscode/settings.json"
            chapter_settings.parent.mkdir()
            chapter_text = '{\n// chapter Rust preferences\n"rust-analyzer.cargo.target":"riscv64gc-unknown-none-elf"\n}\n'
            chapter_settings.write_text(chapter_text)
            copilot.install_hooks(root)
            settings = (root / ".ai/ide/course.code-workspace").read_bytes()
            self.assertEqual(chapter_text, chapter_settings.read_text())
            path = root / copilot.HOOK_LOCATION
            expected = json.loads((REPOSITORY / ".vscode/copilot-hooks.example.json").read_text())
            self.assertEqual(expected, json.loads(path.read_text()))
            config = json.loads(path.read_text())
            config["hooks"]["Stop"].insert(0, {"type": "command", "command": "unrelated"})
            path.write_text(json.dumps(config))
            copilot.install_hooks(root)
            once = path.read_bytes()
            copilot.install_hooks(root)
            self.assertEqual(once, path.read_bytes())
            self.assertEqual(settings, (root / ".ai/ide/course.code-workspace").read_bytes())
            self.assertEqual(chapter_text, chapter_settings.read_text())
            workspace = json.loads(settings)
            self.assertTrue(workspace['settings']['chat.useHooks'])
            self.assertTrue(workspace['settings']['chat.hookFilesLocations']['.github/hooks'])
            self.assertEqual("unrelated", json.loads(once)["hooks"]["Stop"][0]["command"])
            self.assertNotIn("otel", settings.decode())
            for name in ("archive_session.py", "archive_storage.py", "copilot_hook.py"):
                self.assertEqual((SCRIPTS / name).read_bytes(), (root / ".vscode/rcore-hooks" / name).read_bytes())


    def test_cached_script_runs_with_no_plugin_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            copilot.install_hooks(root)
            (root / ".vscode/session-archive.json").write_text('{"enabled":true,"mode":"messages"}')
            source = root / "source.jsonl"
            data = [
                ("session.start", {"sessionId": "cached", "version": 1, "producer": "copilot-agent"}),
                ("user.message", {"content": "CACHED_PROMPT"}),
                ("assistant.turn_start", {"turnId": "0"}),
                ("assistant.message", {"content": "CACHED_REPLY", "toolRequests": []}),
                ("assistant.turn_end", {"turnId": "0"}),
            ]
            stamp = "2026-09-06T13:00:00Z"
            source.write_text("".join(json.dumps({"type": kind, "data": value, "id": str(index), "timestamp": stamp}) + "\n"
                                      for index, (kind, value) in enumerate(data)))
            payload = {"hook_event_name": "Stop", "session_id": "cached", "transcript_path": str(source), "cwd": str(root), "timestamp": stamp}
            result = subprocess.run(["bash", "-c", copilot.COMMAND], cwd=root, input=json.dumps(payload), text=True, capture_output=True)
            self.assertEqual(0, result.returncode)
            self.assertEqual({}, json.loads(result.stdout))
            self.assertFalse((root / "plugins").exists())
            archive = archive_text(session_file(root, "vscode-copilot", "cached"))
            self.assertIn("CACHED_PROMPT", archive)
            self.assertIn("CACHED_REPLY", archive)


    def test_private_files_ignored_and_template_is_not(self):
        result = subprocess.run(["git", "check-ignore", ".github/hooks/rcore-session-archive.json", ".vscode/session-archive.json", ".vscode/rcore-hooks/copilot_hook.py", ".ai/agent-sessions/vscode-copilot/2026-09-09_00-00-00_session.jsonl"], cwd=REPOSITORY, text=True, capture_output=True)
        self.assertEqual(4, len(result.stdout.splitlines()))
        result = subprocess.run(["git", "check-ignore", ".vscode/session-archive.example.json", ".vscode/copilot-hooks.example.json"], cwd=REPOSITORY, capture_output=True)
        self.assertEqual(1, result.returncode)
