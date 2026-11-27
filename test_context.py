import unittest
from unittest.mock import patch

import context
import llm


class ContextTests(unittest.TestCase):
    def test_project_context_uses_the_requested_project_pins(self):
        with patch.object(
            context,
            "load_pins",
            side_effect=lambda project: [f"pin-{project}"],
        ):
            context_a = context.build_context("A", [])
            context_b = context.build_context("B", [])

        self.assertIn("pin-A", context_a["system_extra"])
        self.assertIn("pin-B", context_b["system_extra"])
        self.assertNotIn("pin-B", context_a["system_extra"])
        self.assertNotIn("pin-A", context_b["system_extra"])

    def test_project_context_is_limited_to_its_token_budget(self):
        with patch.object(context, "load_pins", return_value=["x" * 2000]):
            result = context.build_context("A", [])
            project_context = context.project_layer("A")

        self.assertLessEqual(result["stats"]["project_tokens"], 300)
        self.assertLessEqual(len(project_context), 900)

    def test_ask_omits_persistent_context_when_disabled(self):
        with (
            patch.object(
                llm,
                "build_context",
                return_value={
                    "system_extra": "persistent context",
                    "messages": [],
                    "stats": {},
                },
            ),
            patch.object(llm, "run_openai", return_value=("ok", 0)) as run_openai,
        ):
            llm.ask(
                None,
                {"groq": object()},
                [],
                "hello",
                model_override="groq/qwen/qwen3.8-27b",
                include_persistent_context=False,
                allow_tools=False,
            )

        conversation = run_openai.call_args.args[2]
        self.assertEqual(conversation[0]["content"], llm.SYSTEM_INSTRUCTION)
