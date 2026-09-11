"""Local archive regressions adapted from rCore-Tutorial-Code-2025S."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from archive_test_helpers import strings
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
REPOSITORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SCRIPTS))
import archive_session

def transcript_file(directory, records):
    path = directory / "transcript.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path

class CodexFilterTests(unittest.TestCase):
    RECORDS = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "user question"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "developer-secret"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "intermediate-secret"}],
            },
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "call-1",
                "input": "run-command --flag",
                "status": "completed",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-1",
                "output": "tool-output-secret",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": "reasoning-secret",
            },
        },
        {
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "final answer"}],
            },
        },
    ]


    def collect(self, mode):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = transcript_file(Path(temporary_directory), self.RECORDS)
            return list(archive_session.filtered_events(path, "codex", mode))


    def test_messages_mode_keeps_only_user_and_final_messages(self):
        events = self.collect(archive_session.MODE_MESSAGES)
        self.assertEqual(["user", "assistant"], [event["role"] for event in events])
        serialized = json.dumps(events)
        self.assertNotIn("developer-secret", serialized)
        self.assertNotIn("intermediate-secret", serialized)
        self.assertNotIn("run-command", serialized)
        self.assertNotIn("tool-output-secret", serialized)
        self.assertNotIn("reasoning-secret", serialized)


    def test_tool_calls_mode_keeps_call_but_not_output(self):
        events = self.collect(archive_session.MODE_TOOL_CALLS)
        self.assertEqual(
            ["message", "tool_call", "message"],
            [event["type"] for event in events],
        )
        serialized = json.dumps(events)
        self.assertIn("run-command --flag", serialized)
        self.assertNotIn("tool-output-secret", serialized)


class ClaudeFilterTests(unittest.TestCase):
    RECORDS = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "user",
            "isSidechain": False,
            "message": {"role": "user", "content": "user question"},
        },
        {
            "type": "user",
            "isMeta": True,
            "message": {"role": "user", "content": "metadata-secret"},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "stop_reason": "tool_use",
                "content": [{"type": "text", "text": "intermediate-secret"}],
            },
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {"command": "run-command --flag"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "tool-output-secret",
                    }
                ],
            },
        },
        {
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "thinking", "thinking": "reasoning-secret"},
                    {"type": "text", "text": "final answer"},
                ],
            },
        },
    ]


    def collect(self, mode):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = transcript_file(Path(temporary_directory), self.RECORDS)
            return list(
                archive_session.filtered_events(path, "claude-code", mode)
            )


    def test_messages_mode_keeps_only_user_and_final_messages(self):
        events = self.collect(archive_session.MODE_MESSAGES)
        self.assertEqual(["user", "assistant"], [event["role"] for event in events])
        serialized = json.dumps(events)
        self.assertNotIn("metadata-secret", serialized)
        self.assertNotIn("intermediate-secret", serialized)
        self.assertNotIn("run-command", serialized)
        self.assertNotIn("tool-output-secret", serialized)
        self.assertNotIn("reasoning-secret", serialized)


    def test_tool_calls_mode_keeps_call_but_not_result(self):
        events = self.collect(archive_session.MODE_TOOL_CALLS)
        self.assertEqual(
            ["message", "tool_call", "message"],
            [event["type"] for event in events],
        )
        serialized = json.dumps(events)
        self.assertIn("run-command --flag", serialized)
        self.assertNotIn("tool-output-secret", serialized)


class ClaudeStopMessageTests(unittest.TestCase):
    @staticmethod
    def assistant(text, message_id="reply-1", stop_reason="end_turn", **record_fields):
        return {
            "type": "assistant", "isSidechain": False,
            "message": {
                "id": message_id, "role": "assistant", "stop_reason": stop_reason,
                "content": [{"type": "text", "text": text}],
            },
            **record_fields,
        }


    def render(self, records, final_text=None, mode="messages"):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = transcript_file(Path(temporary_directory), records)
            output = io.BytesIO()
            archive_session.write_archive(path, output, "claude-code", mode, "session", final_text)
            self.rendered_events = [json.loads(line) for line in output.getvalue().decode("utf-8").splitlines()][1:]
            return "\n".join(strings(self.rendered_events))


    def test_unflushed_reply_is_present_in_every_archive_mode(self):
        records = ClaudeFilterTests.RECORDS[:-1]
        for mode in archive_session.SUPPORTED_MODES:
            with self.subTest(mode=mode):
                result = self.render(records, "latest reply", mode)
                self.assertEqual(1, result.count("latest reply"))
                self.assertEqual(1, sum(event.get("type") == "message" and event.get("role") == "assistant" for event in self.rendered_events))
                self.assertIn("user question", result)
                self.assertEqual(mode != "messages", "run-command --flag" in result)
                self.assertEqual(mode == "full", "tool-output-secret" in result)
                self.assertEqual(mode == "full", "intermediate-secret" in result)


    def test_flushed_reply_is_not_duplicated_or_changed(self):
        records = [ClaudeFilterTests.RECORDS[0], self.assistant("latest reply")]
        for mode in archive_session.SUPPORTED_MODES:
            with self.subTest(mode=mode):
                self.assertEqual(self.render(records, mode=mode), self.render(records, "latest reply", mode))


    def test_same_answer_to_a_new_prompt_is_not_suppressed(self):
        records = [ClaudeFilterTests.RECORDS[0], self.assistant("same answer"), ClaudeFilterTests.RECORDS[0]]
        for mode in archive_session.SUPPORTED_MODES:
            with self.subTest(mode=mode):
                result = self.render(records, "same answer", mode)
                self.assertEqual(2, result.count("same answer"))
                self.assertEqual(2, sum(event.get("type") == "message" and event.get("role") == "assistant" for event in self.rendered_events))
                self.assertEqual(2, len({event["turn"] for event in self.rendered_events}))


    def test_only_latest_api_message_is_eligible_for_deduplication(self):
        records = [
            ClaudeFilterTests.RECORDS[0],
            self.assistant("repeated text", "earlier"),
            self.assistant("a different continuation", "later"),
        ]
        result = self.render(records, "repeated text")
        self.assertEqual(2, result.count("repeated text"))
        self.assertIn("a different continuation", result)


    def test_tool_result_and_intermediate_text_do_not_suppress_final(self):
        intermediate = self.assistant("latest reply", stop_reason="tool_use")
        intermediate["message"]["content"].append({"type": "tool_use", "name": "Bash", "id": "tool-1", "input": {"command": "pwd"}})
        records = [ClaudeFilterTests.RECORDS[0], intermediate, ClaudeFilterTests.RECORDS[-2]]
        for mode in archive_session.SUPPORTED_MODES:
            with self.subTest(mode=mode):
                result = self.render(records, "latest reply", mode)
                self.assertEqual(1, sum(event.get("type") == "message" and event.get("role") == "assistant" for event in self.rendered_events))
                self.assertEqual(2 if mode == "full" else 1, result.count("latest reply"))


    def test_text_in_a_tool_call_message_is_not_promoted_to_final(self):
        intermediate = self.assistant("latest reply", stop_reason="tool_use")
        intermediate["message"]["content"].append({"type": "tool_use", "name": "Bash", "id": "tool-1", "input": {"command": "pwd"}})
        result = self.render([ClaudeFilterTests.RECORDS[0], intermediate], "latest reply", "full")
        self.assertEqual(1, sum(event.get("type") == "message" and event.get("role") == "assistant" for event in self.rendered_events))
        self.assertIn("intermediate", [event["type"] for event in self.rendered_events])
        self.assertIn("Bash", [event.get("name") for event in self.rendered_events])


    def test_multipart_reply_with_same_message_id_is_deduplicated(self):
        records = [ClaudeFilterTests.RECORDS[0], self.assistant("first block"), self.assistant("second block")]
        for separator in ("", "\n", "\n\n"):
            for mode in archive_session.SUPPORTED_MODES:
                with self.subTest(separator=separator, mode=mode):
                    result = self.render(records, separator.join(("first block", "second block")), mode)
                    self.assertEqual(self.render(records, mode=mode), result)
                    self.assertEqual(1, result.count("first block"))
                    self.assertEqual(1, result.count("second block"))


    def test_partial_text_is_replaced_with_complete_reply(self):
        complete = "回复开始\n\n```rust\nfn main() {}\n```\n\n回复完成"
        partial = self.assistant("回复开始", stop_reason=None)
        partial["message"]["content"].insert(0, {"type": "thinking", "thinking": "readable reasoning"})
        for mode in archive_session.SUPPORTED_MODES:
            with self.subTest(mode=mode):
                result = self.render([ClaudeFilterTests.RECORDS[0], partial], complete, mode)
                self.assertEqual(1, result.count("回复开始"))
                self.assertEqual(1, sum(event.get("type") == "message" and event.get("role") == "assistant" for event in self.rendered_events))
                self.assertIn(complete, result)
                self.assertEqual(mode == "full", "readable reasoning" in result)


    def test_final_text_without_persisted_stop_reason_is_promoted(self):
        records = [ClaudeFilterTests.RECORDS[0], self.assistant("latest reply", stop_reason=None)]
        for mode in archive_session.SUPPORTED_MODES:
            with self.subTest(mode=mode):
                result = self.render(records, "latest reply", mode)
                self.assertEqual(1, result.count("latest reply"))
                self.assertEqual(1, sum(event.get("type") == "message" and event.get("role") == "assistant" for event in self.rendered_events))


    def test_sidechain_reply_does_not_suppress_main_reply(self):
        records = [ClaudeFilterTests.RECORDS[0], self.assistant("latest reply", isSidechain=True)]
        result = self.render(records, "latest reply")
        self.assertEqual(1, result.count("latest reply"))
        self.assertEqual(1, sum(event.get("type") == "message" and event.get("role") == "assistant" for event in self.rendered_events))


    def test_reply_after_interrupted_turn_is_included(self):
        records = [
            *ClaudeFilterTests.RECORDS[:-1],
            {"type": "user", "message": {"role": "user", "content": "[Request interrupted by user]"}},
            {"type": "user", "message": {"role": "user", "content": "continue"}},
        ]
        result = self.render(records, "resumed final reply")
        self.assertIn("continue", result)
        self.assertEqual(1, result.count("resumed final reply"))
        self.assertNotIn("tool-output-secret", result)


    def test_missing_empty_and_non_string_fallbacks_are_ignored(self):
        records = [ClaudeFilterTests.RECORDS[0]]
        for value in (None, "", " \n", [], {}, 123):
            with self.subTest(value=value):
                self.assertEqual(self.render(records), self.render(records, value))


    def test_incomplete_tail_is_tolerated_only_with_stop_reply(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = transcript_file(Path(temporary_directory), [ClaudeFilterTests.RECORDS[0]])
            with path.open("ab") as output:
                output.write(b'{"type":"assistant","message":{"content":"partial')
            original = path.read_bytes()
            for mode in archive_session.SUPPORTED_MODES:
                output = io.BytesIO()
                archive_session.write_archive(path, output, "claude-code", mode, "session", "complete reply")
                self.assertIn(b"complete reply", output.getvalue())
                self.assertEqual(original, path.read_bytes())
            with self.assertRaises(ValueError):
                list(archive_session.read_jsonl_records(path))


    def test_corrupt_complete_line_is_not_hidden_by_stop_reply(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = transcript_file(Path(temporary_directory), [ClaudeFilterTests.RECORDS[0]])
            with path.open("ab") as output:
                output.write(b'{broken JSON}\n')
            with self.assertRaisesRegex(ValueError, "invalid transcript JSON on line 2"):
                list(archive_session.filtered_events(path, "claude-code", "messages", "complete reply"))


class JsonlWriterTests(unittest.TestCase):
    def render(self, records, agent="codex", mode="messages"):
        with tempfile.TemporaryDirectory() as temp:
            path = transcript_file(Path(temp), records)
            output = io.BytesIO()
            archive_session.write_archive(path, output, agent, mode, "session", started_at="2026-09-09T12:34:56Z")
            raw = output.getvalue().decode("utf-8")
            return raw, [json.loads(line) for line in raw.splitlines()]

    def test_header_and_each_event_are_independent_json_objects(self):
        raw, records = self.render(CodexFilterTests.RECORDS)
        self.assertEqual({"type": "session", "schema_version": 1, "agent": "codex", "session_id": "session",
                          "started_at": "2026-09-09T12:34:56+00:00", "mode": "messages"}, records[0])
        self.assertEqual(["user", "assistant"], [event["role"] for event in records[1:]])
        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(len(records), len(raw.splitlines()))

    def test_tool_calls_keep_structured_arguments_and_full_keeps_results(self):
        for agent, source in (("codex", CodexFilterTests.RECORDS), ("claude-code", ClaudeFilterTests.RECORDS)):
            for mode in ("messages", "tool-calls", "full"):
                _, records = self.render(source, agent, mode)
                types = [record["type"] for record in records[1:]]
                self.assertEqual(mode != "messages", "tool_call" in types)
                self.assertEqual(mode == "full", "tool_result" in types)
                self.assertEqual(mode == "full", "reasoning" in types)
                if mode != "messages":
                    call = next(record for record in records if record["type"] == "tool_call")
                    self.assertEqual("Bash" if agent == "claude-code" else "exec", call["name"])

    def test_multiline_unicode_and_large_results_round_trip_without_truncation(self):
        content = '第一行\n{"type":"forged"}\n```rust\nfn main() {}\n```\n' + '结果' * 6000
        source = [{"type": "response_item", "payload": {"type": "message", "role": "user", "content": content}}]
        raw, records = self.render(source)
        self.assertEqual(content, records[1]["content"])
        self.assertEqual(2, len(raw.splitlines()))

    def test_attachments_keep_references_without_embedded_binary(self):
        content = [{"type": "input_text", "text": "look"},
                   {"type": "input_image", "image_url": "data:image/png;base64,BINARY_SECRET"},
                   {"type": "image", "source": {"type": "base64", "data": "SECOND_SECRET"}},
                   {"type": "image", "source": {"type": "url", "url": "https://example.test/image.png"}}]
        raw, records = self.render([{"type": "response_item", "payload": {"type": "message", "role": "user", "content": content}}])
        self.assertNotIn("BINARY_SECRET", raw)
        self.assertNotIn("SECOND_SECRET", raw)
        self.assertEqual("attachment", records[1]["content"][1]["type"])
        self.assertEqual("https://example.test/image.png", records[1]["content"][-1]["url"])

    def test_empty_transcript_has_only_header(self):
        _, records = self.render([])
        self.assertEqual(1, len(records))

    def test_consecutive_user_blocks_and_separate_answers_have_correct_turns(self):
        def message(role, content):
            return {"type": "response_item", "payload": {"type": "message", "role": role, "phase": "final_answer", "content": content}}
        _, records = self.render([message("user", "context"), message("user", "question"), message("assistant", "answer"),
                                  message("user", "next question"), message("assistant", "next answer")])
        self.assertEqual([1, 1, 1, 2, 2], [record["turn"] for record in records[1:]])
