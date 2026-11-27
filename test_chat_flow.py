import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import chat_flow
import memory
from context import thread_layer


class ChatFlowBackgroundTests(unittest.TestCase):
    def test_memory_scan_runs_only_after_every_third_user_turn(self):
        messages = []
        with (
            patch.object(chat_flow, "MEMORY_BACKGROUND_SCAN", True),
            patch.object(chat_flow, "scan_recent_memories") as scan,
        ):
            for turn in range(1, 3):
                messages.extend([
                    {"role": "user", "text": f"user-{turn}"},
                    {"role": "model", "text": f"reply-{turn}"},
                ])
                self.assertIsNone(chat_flow.schedule_memory_scan(
                    None, None, messages, None, None
                ))
            messages.extend([
                {"role": "user", "text": "user-3"},
                {"role": "model", "text": "reply-3"},
            ])
            worker = chat_flow.schedule_memory_scan(
                None, None, messages, None, None
            )
            self.assertIsNotNone(worker)
            worker.join(timeout=5)

        scan.assert_called_once()
        self.assertEqual(len(scan.call_args.args[2]), 6)

    def test_scan_skips_facts_matching_existing_memory_without_punctuation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.json"
            with patch.object(memory, "MEMORY_PATH", path):
                memory.add_memory("Python_", "Tôi dùng Python_")
                reply = json.dumps({
                    "facts": [{
                        "text": "Python.",
                        "source": "Tôi dùng Python",
                    }]
                }, ensure_ascii=False)
                ask = lambda *args, **kwargs: (
                    reply,
                    "gemini/gemini-3.5-flash-lite",
                    {},
                )
                with patch.object(memory, "save_and_verify") as save:
                    memory.scan_recent_memories(
                        None,
                        None,
                        [{"role": "user", "text": "Tôi dùng Python"}],
                        ask,
                        lambda *args: None,
                    )
                self.assertEqual(len(memory.load_memories()), 1)

        save.assert_not_called()

    def test_twenty_messages_are_summarized_and_summary_enters_thread_context(self):
        messages = [
            {"role": "user" if index % 2 == 0 else "model", "text": f"tin-{index}"}
            for index in range(20)
        ]
        persisted = []
        ask_prompts = []

        def ask(*args, **kwargs):
            ask_prompts.append(args[3])
            return "Tóm tắt hội thoại.", "gemini/gemini-3.5-flash-lite", {}

        worker = chat_flow.schedule_thread_summary(
            None,
            None,
            messages,
            "",
            0,
            ask,
            lambda *args: None,
            lambda summary, upto: persisted.append((summary, upto)),
            chat_key="summary-test",
        )
        self.assertIsNotNone(worker)
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(persisted, [("Tóm tắt hội thoại.", 12)])

        context_messages = thread_layer(messages, *persisted[0])
        self.assertEqual(len(context_messages), 9)
        self.assertTrue(context_messages[0]["text"].startswith("Tóm tắt"))
        self.assertEqual(context_messages[1:], messages[12:])

    def test_scan_failure_does_not_prevent_chat_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(memory, "MEMORY_PATH", Path(directory) / "memory.json"),
                patch.object(chat_flow, "MEMORY_BACKGROUND_SCAN", True),
                patch.object(memory, "memory_log"),
            ):
                history = [
                    {"role": "user", "text": "trước đó 1"},
                    {"role": "model", "text": "trả lời 1"},
                    {"role": "user", "text": "trước đó 2"},
                    {"role": "model", "text": "trả lời 2"},
                ]

                def ask(*args, **kwargs):
                    if kwargs.get("model_override"):
                        raise RuntimeError("model scan failure")
                    return "Chat vẫn trả lời.", "gemini/model", {}

                answer, _, _, _ = chat_flow.run_chat_turn(
                    None,
                    None,
                    history,
                    "prompt",
                    "Câu hỏi thường",
                    ask,
                    lambda *args: None,
                )
                history.extend([
                    {"role": "user", "text": "Câu hỏi thường"},
                    {"role": "model", "text": answer},
                ])
                worker = chat_flow.schedule_memory_scan(
                    None, None, history, ask, lambda *args: None
                )
                self.assertIsNotNone(worker)
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(answer, "Chat vẫn trả lời.")


if __name__ == "__main__":
    unittest.main()
