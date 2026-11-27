import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import memory
from memory import build_memory_context, extract_memories


class MemoryExtractionTests(unittest.TestCase):
    def test_discards_source_that_does_not_match_user_text(self):
        reply = json.dumps({
            "facts": [{
                "text": "Đang học ngành CNTT",
                "source": "Tôi đang học ngành CNTT",
            }]
        }, ensure_ascii=False)
        self.assertEqual(
            extract_memories(reply, ["Tôi thích lập trình Python"], []),
            [],
        )

    def test_discards_duplicate_fact(self):
        reply = json.dumps({
            "facts": [{
                "text": "Tôi thích Python",
                "source": "Tôi thích Python",
            }]
        }, ensure_ascii=False)
        existing = [{"text": "  TÔI   thích Python ", "source": "", "id": "", "created": ""}]
        self.assertEqual(
            extract_memories(reply, ["Tôi thích Python"], existing),
            [],
        )

    def test_memory_prompt_context_stays_within_800_characters(self):
        facts = [
            {
                "id": str(index),
                "created": f"2026-01-{index + 1:02d}",
                "text": f"fact-{index:02d} " + "x" * 110,
            }
            for index in range(40)
        ]
        context = build_memory_context(facts)
        self.assertLessEqual(len(context), 800)
        self.assertTrue(context.startswith("THÔNG TIN VỀ NGƯỜI DÙNG"))
        self.assertIn("fact-39", context)
        self.assertNotIn("fact-00", context)

    def test_keeps_the_exact_original_source_substring(self):
        reply = json.dumps({
            "facts": [{
                "text": "Đang học CNTT",
                "source": "tôi học   CNTT",
            }]
        }, ensure_ascii=False)
        facts = extract_memories(reply, ["TÔI  học   CNTT tại trường"], [])
        self.assertEqual(facts[0]["source"], "TÔI  học   CNTT")

    def test_malformed_json_discards_the_whole_extraction(self):
        self.assertIsNone(
            extract_memories('{"facts":[', ["Tôi học CNTT"], [])
        )

    def test_discards_token_like_fact(self):
        token = "A" * 20
        reply = json.dumps({
            "facts": [{
                "text": f"Tôi hay dùng {token}",
                "source": f"Tôi hay dùng {token}",
            }]
        }, ensure_ascii=False)
        self.assertEqual(
            extract_memories(reply, [f"Tôi hay dùng {token}"], []),
            [],
        )

    def test_delete_phrase_is_a_memory_trigger(self):
        self.assertTrue(memory.has_memory_hint("xóa khỏi bộ nhớ mục SQL Server"))


class MemoryChatPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import web

        cls.web = web
        import chat_flow

        cls.chat_flow = chat_flow

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self._patch("MEMORY_PATH", root / "memory" / "global.json", memory)
        self._patch("PROJECTS_DIR", root / "projects", self.web)
        self._patch("LEGACY_CHAT_DIR", root / "legacy-chats", self.web)
        self._patch("MEMORY_BACKGROUND_SCAN", True, self.web)
        self._patch("MEMORY_BACKGROUND_SCAN", True, self.chat_flow)

        self.old_state = {
            "current_project": self.web.current_project,
            "current_chat_id": self.web.current_chat_id,
            "current_extracted_upto": self.web.current_extracted_upto,
            "current_summary": self.web.current_summary,
            "current_summary_upto": self.web.current_summary_upto,
            "messages": self.web.messages,
            "chat_jobs": self.web.chat_jobs,
            "active_job_id": self.web.active_job_id,
            "usage": self.web.usage,
            "memory_extraction_jobs": set(self.web.memory_extraction_jobs),
            "memory_completed_messages": {
                key: set(value)
                for key, value in self.web.memory_completed_messages.items()
            },
            "memory_recent_events": list(self.web.memory_recent_events),
        }
        self.addCleanup(self._restore_web_state)
        self.web.current_project = "default"
        self.web.current_chat_id = None
        self.web.current_extracted_upto = 0
        self.web.messages = [
            {"role": "user", "text": "trước đó 1"},
            {"role": "model", "text": "trả lời 1"},
            {"role": "user", "text": "trước đó 2"},
            {"role": "model", "text": "trả lời 2"},
        ]
        self.web.current_summary = ""
        self.web.current_summary_upto = 0
        self.web.chat_jobs = {}
        self.web.active_job_id = "memory-test-job"
        self.web.usage = {"models": {}}
        self.web.memory_completed_messages.clear()
        self.web.memory_extraction_jobs.clear()
        self.web.memory_recent_events.clear()

    def _patch(self, attribute, value, target):
        patcher = patch.object(target, attribute, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore_web_state(self):
        for name in (
            "current_project",
            "current_chat_id",
            "current_extracted_upto",
            "current_summary",
            "current_summary_upto",
            "messages",
            "chat_jobs",
            "active_job_id",
            "usage",
        ):
            value = self.old_state[name]
            setattr(self.web, name, value)
        self.web.memory_extraction_jobs.clear()
        self.web.memory_extraction_jobs.update(
            self.old_state["memory_extraction_jobs"]
        )
        self.web.memory_completed_messages.clear()
        self.web.memory_completed_messages.update(
            self.old_state["memory_completed_messages"]
        )
        self.web.memory_recent_events.clear()
        self.web.memory_recent_events.extend(self.old_state["memory_recent_events"])

    def _run_chat_turn(self, user_message, extraction_reply, expected_workers=1):
        worker_threads = []
        model_overrides = []
        extraction_prompts = []
        real_thread = threading.Thread

        def fake_ask(*args, **kwargs):
            model_overrides.append(kwargs.get("model_override"))
            if kwargs.get("model_override") == "gemini/gemini-3.5-flash-lite":
                extraction_prompts.append(args[3])
                return extraction_reply, "gemini/gemini-3.5-flash-lite", {}
            return "Câu trả lời thử nghiệm.", "gemini/gemini-3.5-flash", {}

        def tracked_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            worker_threads.append(thread)
            return thread

        with (
            patch.object(self.web, "ask", side_effect=fake_ask),
            patch.object(self.web, "record_usage"),
            patch.object(self.web, "save_history"),
            patch.object(self.web, "_workspace_file_mtimes", return_value={}),
            patch.object(self.web.threading, "Thread", side_effect=tracked_thread),
        ):
            self.web._run_chat_job("memory-test-job", user_message)
            for thread in worker_threads:
                thread.join(timeout=5)
                self.assertFalse(
                    thread.is_alive(), "memory extraction worker did not finish"
                )
        self.assertEqual(len(worker_threads), expected_workers)
        if expected_workers:
            self.assertIn("gemini/gemini-3.5-flash-lite", model_overrides)
            self.assertIn(user_message, extraction_prompts[0])

    def test_chat_turn_extracts_multiple_long_term_facts(self):
        reply = json.dumps({
            "facts": [
                {"text": "Học CNTT năm 3", "source": "tôi học CNTT năm 3"},
                {
                    "text": "Quen dùng Python 3.12 và Kotlin",
                    "source": "quen dùng Python 3.12 và Kotlin",
                },
                {
                    "text": "Thích câu trả lời ngắn gọn",
                    "source": "tôi thích câu trả lời ngắn gọn",
                },
            ]
        }, ensure_ascii=False)
        self._run_chat_turn(
            "tôi học CNTT năm 3, quen dùng Python 3.12 và Kotlin, "
            "tôi thích câu trả lời ngắn gọn",
            reply,
        )
        self.assertGreaterEqual(len(memory.load_memories()), 2)

    def test_chat_turn_does_not_save_temporary_state(self):
        self._run_chat_turn(
            "hôm nay tôi đang mệt",
            json.dumps({"facts": []}),
        )
        self.assertEqual(memory.load_memories(), [])

    def test_explicit_remember_request_saves_fact(self):
        reply = json.dumps({
            "facts": [{
                "text": "Dùng SQL Server",
                "source": "tôi dùng SQL Server",
            }]
        }, ensure_ascii=False)
        with patch.object(self.chat_flow, "MEMORY_BACKGROUND_SCAN", False):
            self._run_chat_turn(
                "nhớ giúp tôi rằng tôi dùng SQL Server",
                reply,
                expected_workers=0,
            )
        self.assertEqual(
            [item["text"] for item in memory.load_memories()],
            ["tôi dùng SQL Server"],
        )

    def test_natural_forget_request_deletes_only_selected_memory(self):
        kept, _ = memory.add_memory("Dùng PostgreSQL", "Tôi dùng PostgreSQL")
        removed, _ = memory.add_memory("Dùng SQL Server", "Tôi dùng SQL Server")

        self._run_chat_turn(
            "quên chuyện SQL Server đi",
            json.dumps({"delete_ids": [removed["id"]]}),
        )

        self.assertEqual(
            [item["id"] for item in memory.load_memories()],
            [kept["id"]],
        )


if __name__ == "__main__":
    unittest.main()
