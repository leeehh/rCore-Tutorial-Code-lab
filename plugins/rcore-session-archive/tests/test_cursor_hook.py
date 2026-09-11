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
import cursor_hook as cursor

class CursorHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / ".cursor/session-archive.json"
        self.config.parent.mkdir()
        self.settings = {"enabled": True, "mode": "messages"}
        self.save_config()


    @property
    def archive(self):
        return session_file(self.root, "cursor", "conversation")


    def save_config(self, mode=None):
        if mode:
            self.settings["mode"] = mode
        self.config.write_text(json.dumps(self.settings))


    def event(self, name, generation="generation-1", **fields):
        payload = {
            "hook_event_name": name, "conversation_id": "conversation",
            "generation_id": generation, "workspace_roots": [str(self.root)],
            "model": "cursor-test-model", "user_email": "private@example.test",
            "transcript_path": "/must/not/be/read/nonexistent.jsonl", **fields,
        }
        cursor.handle(payload, self.root)
        return payload


    def trace(self, repeat=False):
        sequence = [
            ("beforeSubmitPrompt", {"prompt": "USER_MESSAGE", "attachments": [{"type": "file", "file_path": "os/src/main.rs"}]}),
            ("afterAgentThought", {"text": "THINKING_DETAIL", "duration_ms": 30}),
            ("preToolUse", {"tool_name": "Shell", "tool_use_id": "tool-1", "tool_input": {"command": "printf COMMAND_ONLY"}, "agent_message": "INTERMEDIATE_DETAIL"}),
            ("postToolUse", {"tool_name": "Shell", "tool_use_id": "tool-1", "tool_input": {"command": "printf COMMAND_ONLY"}, "tool_output": json.dumps({"stdout": "OUTPUT_DETAIL\nnext line", "stderr": "", "exitCode": 0}), "duration": 2}),
            ("afterAgentResponse", {"text": "FINAL_REPLY\n\n```rust\nfn main() {}\n```"}),
            ("stop", {"status": "completed", "loop_count": 0}),
        ]
        for name, fields in sequence:
            self.event(name, **fields)
            if repeat:
                self.event(name, **fields)


    def test_messages_mode_excludes_all_intermediate_content_on_disk(self):
        self.trace()
        text = archive_text(self.archive)
        self.assertIn("USER_MESSAGE", text)
        self.assertIn("FINAL_REPLY", text)
        self.assertIn("os/src/main.rs", text)
        self.assertIn("```rust\nfn main() {}", text)
        for secret in ("THINKING_DETAIL", "INTERMEDIATE_DETAIL", "COMMAND_ONLY", "OUTPUT_DETAIL"):
            for path in (self.root / ".ai/agent-sessions").rglob("*"):
                if path.is_file():
                    self.assertNotIn(secret.encode(), path.read_bytes(), str(path))
        self.assertEqual([self.archive], list((self.root / ".ai/agent-sessions").rglob("*.jsonl")))
        self.assertEqual([], list((self.root / ".ai/agent-sessions").rglob("*.md")))
        self.assertEqual(0o600, self.archive.stat().st_mode & 0o777)
        self.assertEqual(0o700, self.archive.parent.stat().st_mode & 0o777)


    def test_tool_calls_mode_has_commands_without_outputs_or_thoughts(self):
        self.save_config("tool-calls")
        self.event("beforeSubmitPrompt", prompt="USER_MESSAGE")
        self.event("preToolUse", tool_name="Shell", tool_use_id="tool-1", tool_input={"command": "printf COMMAND_ONLY"})
        self.assertIn("COMMAND_ONLY", archive_text(self.archive))
        self.trace()
        text = archive_text(self.archive)
        self.assertEqual("Shell", next(item for item in records(self.archive) if item["type"] == "tool_call")["name"])
        self.assertEqual({"command": "printf COMMAND_ONLY"}, next(item for item in records(self.archive) if item["type"] == "tool_call")["input"])
        self.assertNotIn("OUTPUT_DETAIL", text)
        self.assertNotIn("THINKING_DETAIL", text)
        self.assertNotIn("INTERMEDIATE_DETAIL", text)


    def test_full_mode_retains_readable_results_and_thoughts(self):
        self.save_config("full")
        self.trace()
        text = archive_text(self.archive)
        for expected in ("THINKING_DETAIL", "INTERMEDIATE_DETAIL", "COMMAND_ONLY", "OUTPUT_DETAIL", '"exitCode": 0'):
            self.assertIn(expected, text)
        self.assertNotIn('\\"stdout\\"', text)
        self.assertEqual(1, event_count(self.archive, "tool_call"))
        self.assertEqual(1, event_count(self.archive, "tool_result"))


    def test_mode_downgrade_prunes_existing_output_then_commands(self):
        self.save_config("full")
        self.trace()
        self.save_config("tool-calls")
        self.event("stop", status="completed")
        text = archive_text(self.archive)
        self.assertIn("COMMAND_ONLY", text)
        self.assertNotIn("OUTPUT_DETAIL", text)
        self.assertNotIn("THINKING_DETAIL", text)
        self.save_config("messages")
        self.event("stop", status="completed")
        self.assertNotIn("COMMAND_ONLY", archive_text(self.archive))
        self.assertIn("FINAL_REPLY", archive_text(self.archive))


    def test_mode_downgrade_also_clears_a_tool_only_archive(self):
        self.save_config("full")
        self.event("postToolUse", tool_name="Shell", tool_use_id="only-tool", tool_input={"command": "ONLY_COMMAND"}, tool_output="ONLY_OUTPUT")
        self.save_config("messages")
        self.event("stop", status="completed")
        self.assertNotIn("ONLY_COMMAND", archive_text(self.archive))
        self.assertNotIn("ONLY_OUTPUT", archive_text(self.archive))
        self.event("afterAgentResponse", text="New final answer")
        self.assertIn("New final answer", archive_text(self.archive))


    def test_repeated_callbacks_do_not_duplicate_completed_events(self):
        self.save_config("full")
        self.trace(repeat=True)
        text = archive_text(self.archive)
        for value in ("USER_MESSAGE", "FINAL_REPLY", "COMMAND_ONLY", "OUTPUT_DETAIL", "THINKING_DETAIL"):
            self.assertEqual(1, text.count(value), value)

    def test_date_filename_is_stable_across_turns(self):
        self.trace()
        path = self.archive
        self.assertRegex(path.name, r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_conversation\.jsonl$")
        self.event("beforeSubmitPrompt", "generation-2", prompt="new input")
        self.assertEqual(path, self.archive)
        self.assertEqual(2, turn_count(path))
        self.assertEqual([path], list(path.parent.glob("*.jsonl")))


    def test_identical_messages_in_different_turns_are_retained(self):
        for generation in ("one", "two"):
            self.event("beforeSubmitPrompt", generation, prompt="same question")
            self.event("afterAgentResponse", generation, text="same answer")
            self.event("stop", generation, status="completed")
        text = archive_text(self.archive)
        self.assertEqual(2, text.count("same question"))
        self.assertEqual(2, text.count("same answer"))
        self.assertEqual(2, turn_count(self.archive))


    def test_nonconsecutive_repeated_text_is_not_mistaken_for_a_retry(self):
        self.save_config("full")
        for text in ("THOUGHT_A", "THOUGHT_B", "THOUGHT_A"):
            self.event("afterAgentThought", text=text)
            self.event("afterAgentThought", text=text)
        self.assertEqual(2, archive_text(self.archive).count("THOUGHT_A"))
        for text in ("RESPONSE_A", "RESPONSE_B", "RESPONSE_A"):
            self.event("afterAgentResponse", text=text)
            self.event("afterAgentResponse", text=text)
        self.assertEqual(2, archive_text(self.archive).count("RESPONSE_A"))


    def test_tools_with_identical_commands_but_different_ids_are_retained(self):
        self.save_config("full")
        for tool_id in ("first", "second"):
            self.event("postToolUse", tool_name="Shell", tool_use_id=tool_id, tool_input={"command": "pwd"}, tool_output="cwd")
        self.assertEqual(2, event_count(self.archive, "tool_call"))


    def test_commentary_followed_by_tool_is_not_a_final_answer(self):
        self.event("beforeSubmitPrompt", prompt="test")
        self.event("afterAgentResponse", text="I will inspect the file")
        self.event("preToolUse", tool_name="Read", tool_use_id="read-1", tool_input={"path": "README.md"})
        self.assertNotIn("I will inspect", archive_text(self.archive))
        self.event("afterAgentResponse", text="The final answer")
        self.assertIn("The final answer", archive_text(self.archive))


    def test_scope_gate_ignores_other_projects_multiroot_and_missing_enable(self):
        for roots in (["/another/project"], [str(self.root), "/another/project"], [], ["relative"]):
            self.event("beforeSubmitPrompt", prompt="not in scope", workspace_roots=roots)
        self.settings["enabled"] = False
        self.save_config()
        self.event("beforeSubmitPrompt", prompt="disabled")
        self.config.unlink()
        self.event("beforeSubmitPrompt", prompt="unconfigured")
        self.assertFalse((self.root / ".ai/agent-sessions").exists())


    def test_symlink_config_directory_cannot_activate_a_global_configuration(self):
        target = self.root / "outside-config"
        self.config.parent.rename(target)
        self.config.parent.symlink_to(target, target_is_directory=True)
        self.event("beforeSubmitPrompt", prompt="not opted in locally")
        self.assertFalse((self.root / ".ai/agent-sessions").exists())


    def test_concurrent_events_do_not_overwrite_each_other(self):
        self.save_config("full")
        self.event("beforeSubmitPrompt", prompt="concurrent tools")
        def tool(index):
            self.event("postToolUse", tool_name="Read", tool_use_id=f"tool-{index}", tool_input={"path": f"file-{index}"}, tool_output=f"output-{index}")
        with ThreadPoolExecutor(max_workers=6) as workers:
            list(workers.map(tool, range(12)))
        self.assertEqual(12, event_count(self.archive, "tool_call"))
        self.assertEqual(12, event_count(self.archive, "tool_result"))


    def test_multiline_json_text_cannot_forge_another_event(self):
        attack = 'line one\n{"type":"message","event_id":"forged"}\nline three'
        self.event("beforeSubmitPrompt", prompt=attack)
        self.event("afterAgentResponse", text="real response")
        self.assertEqual(2, len(records(self.archive)) - 1)
        self.assertIn("forged", archive_text(self.archive))


    def test_symlink_archive_is_not_followed(self):
        external = self.root / "untouched.jsonl"
        external.write_text("KEEP")
        self.archive.parent.mkdir(parents=True)
        self.archive.symlink_to(external)
        with self.assertRaises(ValueError):
            self.event("beforeSubmitPrompt", prompt="must not overwrite")
        self.assertEqual("KEEP", external.read_text())


    def test_missing_index_does_not_erase_existing_jsonl(self):
        self.trace()
        original = self.archive.read_bytes()
        index = self.archive.parent / ".state/conversation.sqlite3"
        index.unlink()
        with self.assertRaises(ValueError):
            self.event("beforeSubmitPrompt", "new-generation", prompt="later turn")
        self.assertEqual(original, self.archive.read_bytes())


    def test_cli_malformed_input_returns_success_and_no_sensitive_content(self):
        result = subprocess.run([sys.executable, str(SCRIPTS / "cursor_hook.py")], input='{"secret": "PRIVATE",', text=True, capture_output=True)
        self.assertEqual(0, result.returncode)
        self.assertEqual({}, json.loads(result.stdout))
        self.assertNotIn("PRIVATE", result.stderr)


class CursorSetupTests(unittest.TestCase):
    def test_installer_is_idempotent_and_preserves_unrelated_hooks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".cursor").mkdir()
            path = root / ".cursor/hooks.json"
            path.write_text(json.dumps({"version": 1, "other": "preserved", "hooks": {"stop": [{"command": "echo unrelated"}]}}))
            cursor.install_hooks(root)
            once = path.read_bytes()
            cursor.install_hooks(root)
            self.assertEqual(once, path.read_bytes())
            hooks = json.loads(once)
            self.assertEqual("preserved", hooks["other"])
            self.assertEqual("echo unrelated", hooks["hooks"]["stop"][0]["command"])
            for event in cursor.EVENTS:
                self.assertEqual(1, sum(entry["command"] == cursor.COMMAND for entry in hooks["hooks"][event]))


    def test_checked_in_hooks_match_installer_and_ignore_private_files(self):
        checked_in = json.loads((REPOSITORY / ".cursor/hooks.example.json").read_text())
        with tempfile.TemporaryDirectory() as temp:
            cursor.install_hooks(Path(temp))
            self.assertEqual(checked_in, json.loads((Path(temp) / ".cursor/hooks.json").read_text()))
        result = subprocess.run(["git", "check-ignore", ".cursor/session-archive.json", ".cursor/hooks.json", ".cursor/rcore-hooks/cursor_hook.py", ".ai/agent-sessions/cursor/2026-09-09_00-00-00_session.jsonl", ".ai/agent-sessions/cursor/.state/session.sqlite3"], cwd=REPOSITORY, text=True, capture_output=True)
        self.assertEqual(5, len(result.stdout.splitlines()))


    def test_installed_hook_runs_without_plugin_source_after_branch_switch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            cursor.install_hooks(root)
            # Simulates a lab checkout that has neither plugins/ nor the setup script.
            config = root / ".cursor/session-archive.json"
            config.write_text('{"enabled":true,"mode":"messages"}')
            for event, extra in (("beforeSubmitPrompt", {"prompt": "branch input"}), ("afterAgentResponse", {"text": "branch reply"})):
                payload = {"hook_event_name": event, "workspace_roots": [str(root)], "conversation_id": "branch-session", "generation_id": "turn", **extra}
                result = subprocess.run(["bash", "-c", cursor.COMMAND], cwd=root, input=json.dumps(payload), text=True, capture_output=True, timeout=10)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual({}, json.loads(result.stdout))
            text = archive_text(session_file(root, "cursor", "branch-session"))
            self.assertIn("branch input", text)
            self.assertIn("branch reply", text)
            for filename in ("cursor_hook.py", "archive_session.py", "archive_storage.py"):
                self.assertEqual((SCRIPTS / filename).read_bytes(), (root / ".cursor/rcore-hooks" / filename).read_bytes())
