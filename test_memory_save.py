import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import chat_flow
import llm
import memory


class SaveAndVerifyTests(unittest.TestCase):
    def test_chat_context_forbids_unverified_claims_and_reports_failed_forget(self):
        conversations = []

        def fake_openai(client, model, conversation, **kwargs):
            conversations.append(conversation)
            return "Trả lời.", 0

        with (
            patch.object(
                llm,
                "available_models",
                return_value=[("groq", "qwen/qwen3.8-27b")],
            ),
            patch.object(llm, "run_openai", side_effect=fake_openai),
        ):
            for intent, forget_failed in (("ordinary", False), ("forget", True)):
                llm.ask(
                    None,
                    {"groq": object()},
                    [],
                    "Câu hỏi",
                    include_persistent_context=False,
                    allow_tools=False,
                    memory_intent=intent,
                    memory_forget_failed=forget_failed,
                )

        ordinary_system = conversations[0][0]["content"]
        forget_system = conversations[1][0]["content"]
        self.assertIn(
            "Không được nói đã ghi nhận/đã lưu/đã nhớ/đã quên; "
            "hệ thống tự hiển thị 🧠 khi thật sự lưu.",
            ordinary_system,
        )
        self.assertIn(
            "Việc xóa chưa thực hiện được, hãy nói thật là chưa xóa.",
            forget_system,
        )

    def test_remember_prefix_and_leading_colon_are_removed_before_saving(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "global.json"
            with patch.object(memory, "MEMORY_PATH", path):
                result = chat_flow.run_chat_turn(
                    None,
                    {},
                    [],
                    "nhớ giúp tôi: tôi dùng SQL Server",
                    "nhớ giúp tôi: tôi dùng SQL Server",
                    ask_fn=lambda *args, **kwargs: (
                        "Được.",
                        "gemini/gemini-3.5-flash",
                        {},
                    ),
                    record_usage_fn=lambda model, meta: None,
                )
                saved = memory.load_memories()

        self.assertTrue(result[3]["saved"])
        self.assertEqual(saved[0]["text"], "tôi dùng SQL Server")

    def test_remember_request_saves_and_reads_back_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "global.json"
            with patch.object(memory, "MEMORY_PATH", path):
                result = memory.save_and_verify("Tôi dùng PostgreSQL")
                saved = memory.load_memories()

        self.assertTrue(result["saved"])
        self.assertEqual(result["id"], saved[0]["id"])
        self.assertEqual(saved[0]["text"], "Tôi dùng PostgreSQL")
        self.assertIsNone(result["error"])

    def test_write_failure_is_passed_to_system_context(self):
        captured_calls = []
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(memory, "MEMORY_PATH", Path(temp_dir) / "global.json"),
                patch.object(memory, "add_memory", side_effect=OSError("disk full")),
                patch.object(
                    llm,
                    "available_models",
                    return_value=[("groq", "qwen/qwen3.8-27b")],
                ),
                patch.object(
                    llm,
                    "run_openai",
                    side_effect=lambda client, model, conversation, **kwargs: (
                        captured_calls.append(conversation) or ("Chưa lưu được.", 0)
                    ),
                ),
            ):
                result = chat_flow.run_chat_turn(
                    None,
                    {"groq": object()},
                    [],
                    "hãy nhớ rằng tôi dùng PostgreSQL",
                    "hãy nhớ rằng tôi dùng PostgreSQL",
                    ask_fn=llm.ask,
                    record_usage_fn=lambda model, meta: None,
                )

        self.assertFalse(result[3]["saved"])
        self.assertIn("disk full", result[3]["error"])
        self.assertIn(
            "Lưu thất bại, hãy nói thật là chưa lưu được",
            captured_calls[0][0]["content"],
        )

    def test_ordinary_message_does_not_touch_memory(self):
        ask_calls = []

        def fake_ask(*args, **kwargs):
            ask_calls.append(kwargs)
            return "Trả lời.", "groq/qwen/qwen3.8-27b", None

        with patch.object(
            chat_flow,
            "save_and_verify",
            side_effect=AssertionError("unexpected memory write"),
        ):
            result = chat_flow.run_chat_turn(
                None,
                {},
                [],
                "Tôi thích trà",
                "Tôi thích trà",
                ask_fn=fake_ask,
                record_usage_fn=lambda model, meta: None,
            )

        self.assertIsNone(result[3])
        self.assertIsNone(ask_calls[0]["memory_saved"])


if __name__ == "__main__":
    unittest.main()
