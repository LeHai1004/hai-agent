import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import artifacts


class ArtifactPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_directory = tempfile.TemporaryDirectory()
        cls.original_directory = os.getcwd()
        os.chdir(cls.temp_directory.name)
        import web
        cls.web = web
        os.chdir(cls.original_directory)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.original_directory)
        cls.temp_directory.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.project_root = root / "projects"
        self.current_project_path = root / "current_project.txt"
        self.current_project_path.write_text("project-a", encoding="utf-8")
        self.project_patches = (
            patch.object(artifacts, "PROJECTS_DIR", self.project_root),
            patch.object(
                artifacts,
                "CURRENT_PROJECT_PATH",
                self.current_project_path,
            ),
        )
        for project_patch in self.project_patches:
            project_patch.start()
        self.addCleanup(self.directory.cleanup)
        for project_patch in reversed(self.project_patches):
            self.addCleanup(project_patch.stop)

    def test_markdown_blocks_rejoin_original_with_exact_offsets(self):
        content = (
            "\n# Guide\n\nA paragraph\ncontinued.\n\n"
            "- first item\n- second item\n\n"
            "```python\nprint('hello')\n```\n"
        )
        blocks = artifacts.split_markdown_blocks(content)

        self.assertEqual(
            [block["type"] for block in blocks],
            ["heading", "paragraph", "list", "code"],
        )
        self.assertEqual("".join(block["content"] for block in blocks), content)
        self.assertEqual(blocks[0]["start"], 0)
        self.assertEqual(blocks[-1]["end"], len(content))
        for index, block in enumerate(blocks):
            self.assertEqual(
                block["content"],
                content[block["start"]:block["end"]],
            )
            if index:
                self.assertEqual(blocks[index - 1]["end"], block["start"])

    def test_apply_patch_updates_only_selected_range_and_adds_version(self):
        content = "# Title\nKeep this sentence.\nEnd."
        artifact = artifacts.create_artifact(
            "Guide",
            "markdown",
            content,
            project="project-a",
        )
        start = content.index("Keep this sentence.")
        end = start + len("Keep this sentence.")

        updated = artifacts.apply_patch(
            artifact["id"],
            1,
            start,
            end,
            "Keep this updated.",
            "bot",
            project="project-a",
        )
        expected = content[:start] + "Keep this updated." + content[end:]

        self.assertEqual(updated["content"], expected)
        self.assertEqual(updated["versions"][-1]["number"], 2)
        self.assertEqual(updated["versions"][-1]["source"], "bot")
        self.assertEqual(updated["content"][:start], content[:start])
        self.assertEqual(
            updated["content"][start + len("Keep this updated."):],
            content[end:],
        )

    def test_old_base_version_is_rejected_without_adding_a_version(self):
        artifact = artifacts.create_artifact(
            "Guide",
            "markdown",
            "before selected after",
            project="project-a",
        )
        artifacts.apply_patch(
            artifact["id"],
            1,
            7,
            15,
            "changed",
            "user",
            project="project-a",
        )
        current = artifacts.get_artifact(artifact["id"], "project-a")

        with self.assertRaises(artifacts.ArtifactVersionConflict):
            artifacts.apply_patch(
                artifact["id"],
                1,
                7,
                14,
                "stale",
                "bot",
                project="project-a",
            )

        after = artifacts.get_artifact(artifact["id"], "project-a")
        self.assertEqual(after, current)
        self.assertEqual(len(after["versions"]), 2)

    def test_patch_route_uses_server_selected_text_and_300_char_context(self):
        content = "A" * 300 + "old" + "B" * 300
        artifact = artifacts.create_artifact(
            "Guide",
            "markdown",
            content,
            project="project-a",
        )
        captured = {}

        def fake_ask(*args, **kwargs):
            captured["prompt"] = args[3]
            captured["kwargs"] = kwargs
            return "new", "fake/model", None

        with (
            patch.object(self.web, "current_project", "project-a"),
            patch.object(self.web, "ask", side_effect=fake_ask) as ask,
        ):
            response = self.web.app.test_client().post(
                f"/api/artifacts/{artifact['id']}/patch",
                json={
                    "base_version": 1,
                    "start": 300,
                    "end": 303,
                    "instruction": "clarify this phrase",
                    "selected_text": "client-forged-text",
                },
            )

        expected = "A" * 300 + "new" + "B" * 300
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["version"], 2)
        self.assertEqual(response.json["content"], expected)
        self.assertEqual(response.json["start"], 300)
        self.assertEqual(response.json["end_moi"], 303)
        self.assertEqual(ask.call_count, 1)
        self.assertIn("A" * 300, captured["prompt"])
        self.assertIn("B" * 300, captured["prompt"])
        self.assertIn("old", captured["prompt"])
        self.assertNotIn("client-forged-text", captured["prompt"])
        self.assertFalse(captured["kwargs"]["allow_tools"])
        self.assertFalse(captured["kwargs"]["use_history"])

    def test_code_patch_keeps_language_and_does_not_add_fences(self):
        content = "def add(a, b):\n    return a - b\n"
        artifact = artifacts.create_artifact(
            "Adder",
            "code",
            content,
            language="python",
            project="project-a",
        )
        start = content.index("    return")
        end = len(content.rstrip("\n"))
        prompt = {}

        def fake_ask(*args, **_kwargs):
            prompt["text"] = args[3]
            return "    return a + b", "fake/model", None

        with (
            patch.object(self.web, "current_project", "project-a"),
            patch.object(self.web, "ask", side_effect=fake_ask),
        ):
            response = self.web.app.test_client().post(
                f"/api/artifacts/{artifact['id']}/patch",
                json={
                    "base_version": 1,
                    "start": start,
                    "end": end,
                    "instruction": "use addition",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json["content"],
            "def add(a, b):\n    return a + b\n",
        )
        self.assertIn("code, ngôn ngữ python", prompt["text"])
        self.assertIn("không bọc thêm ```", prompt["text"])
        self.assertNotIn("```", response.json["content"])

    def test_patch_route_rejects_stale_or_cross_project_versions(self):
        artifact = artifacts.create_artifact(
            "Guide",
            "markdown",
            "before selected after",
            project="project-a",
        )
        client = self.web.app.test_client()
        payload = {
            "base_version": 2,
            "start": 7,
            "end": 15,
            "instruction": "edit it",
        }

        with (
            patch.object(self.web, "current_project", "project-a"),
            patch.object(self.web, "ask") as ask,
        ):
            stale = client.post(
                f"/api/artifacts/{artifact['id']}/patch",
                json=payload,
            )
        self.assertEqual(stale.status_code, 409)
        ask.assert_not_called()

        payload["base_version"] = 1
        with (
            patch.object(self.web, "current_project", "project-b"),
            patch.object(self.web, "ask") as ask,
        ):
            cross_project = client.post(
                f"/api/artifacts/{artifact['id']}/patch",
                json=payload,
            )
        self.assertEqual(cross_project.status_code, 404)
        ask.assert_not_called()
        self.assertEqual(
            artifacts.get_artifact(artifact["id"], "project-a")["content"],
            "before selected after",
        )

    def test_model_error_or_empty_reply_does_not_create_version(self):
        artifact = artifacts.create_artifact(
            "Guide",
            "markdown",
            "before selected after",
            project="project-a",
        )
        payload = {
            "base_version": 1,
            "start": 7,
            "end": 15,
            "instruction": "edit it",
        }
        client = self.web.app.test_client()
        model_responses = (
            {"side_effect": RuntimeError("model unavailable")},
            {"return_value": ("   ", "fake/model", None)},
        )
        for model_response in model_responses:
            with (
                patch.object(self.web, "current_project", "project-a"),
                patch.object(self.web, "ask", **model_response),
            ):
                response = client.post(
                    f"/api/artifacts/{artifact['id']}/patch",
                    json=payload,
                )
            self.assertEqual(response.status_code, 502)
            self.assertIn("error", response.json)
            current = artifacts.get_artifact(artifact["id"], "project-a")
            self.assertEqual(current["content"], "before selected after")
            self.assertEqual(len(current["versions"]), 1)


if __name__ == "__main__":
    unittest.main()
