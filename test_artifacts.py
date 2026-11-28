import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import artifacts
import tools


class ArtifactTests(unittest.TestCase):
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
        self.root = Path(self.directory.name)
        self.project_root = self.root / "projects"
        self.current_project_path = self.root / "current_project.txt"
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

    def test_create_get_update_and_keep_only_latest_twenty_versions(self):
        created = artifacts.create_artifact(
            "Design notes",
            "markdown",
            "# v1",
            project="project-a",
        )
        self.assertEqual(created["versions"][0]["number"], 1)
        self.assertEqual(created["versions"][0]["source"], "bot")
        self.assertNotIn("language", created)
        self.assertEqual(
            artifacts.get_artifact(created["id"], "project-a"),
            created,
        )

        updated = created
        for version in range(2, 23):
            updated = artifacts.update_artifact(
                created["id"],
                f"# v{version}",
                project="project-a",
                source="user" if version % 2 == 0 else "bot",
            )

        self.assertEqual(len(updated["versions"]), 20)
        self.assertEqual(updated["versions"][0]["number"], 3)
        self.assertEqual(updated["versions"][-1]["number"], 22)
        self.assertEqual(updated["content"], "# v22")
        self.assertEqual(updated["versions"][-1]["source"], "user")
        self.assertEqual(
            updated["versions"][-1]["created"],
            updated["updated"],
        )

    def test_code_language_is_only_stored_for_code(self):
        code = artifacts.create_artifact(
            "Example",
            "code",
            "print('hello')",
            language="python",
            project="project-a",
        )
        self.assertEqual(code["language"], "python")
        with self.assertRaises(ValueError):
            artifacts.create_artifact(
                "Invalid markdown",
                "markdown",
                "text",
                language="python",
                project="project-a",
            )

    def test_artifact_routes_are_scoped_to_current_project(self):
        item = artifacts.create_artifact(
            "Project A",
            "markdown",
            "private content",
            project="project-a",
        )
        artifacts.create_artifact(
            "Project B",
            "markdown",
            "other content",
            project="project-b",
        )
        client = self.web.app.test_client()
        with patch.object(self.web, "current_project", "project-a"):
            listing = client.get("/api/artifacts")
            detail = client.get(f"/api/artifacts/{item['id']}")
            self.assertEqual(listing.status_code, 200)
            self.assertEqual(
                [entry["id"] for entry in listing.json["artifacts"]],
                [item["id"]],
            )
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json["artifact"]["content"], "private content")
            self.assertEqual(
                client.get(
                    f"/api/artifacts/{'0' * 32}"
                ).status_code,
                404,
            )
        with patch.object(self.web, "current_project", "project-b"):
            self.assertEqual(
                client.get(f"/api/artifacts/{item['id']}").status_code,
                404,
            )

    def test_chat_history_saves_only_artifact_reference(self):
        messages = []
        artifact_ref = {
            "id": "a" * 32,
            "title": "Long guide",
            "artifact_type": "markdown",
        }

        def fake_stream(*_args, **_kwargs):
            yield {"type": "token", "text": "very long document body"}
            yield {
                "type": "final",
                "reply": "very long document body",
                "model": "fake/model",
                "_meta": None,
                "memory_result": None,
            }

        with (
            patch.object(self.web, "messages", messages),
            patch.object(self.web, "run_chat_turn_stream", fake_stream),
            patch.object(self.web, "save_history"),
            patch.object(self.web, "format_usage", return_value="usage"),
            patch.object(self.web, "load_usage", return_value={}),
        ):
            result = self.web._process_chat_message(
                "Create a guide",
                event_sink=lambda _event: None,
                artifact_refs=[artifact_ref],
            )

        self.assertEqual(
            messages[-1]["text"],
            f"Artifact: {artifact_ref['title']} (id: {artifact_ref['id']})",
        )
        self.assertNotIn("very long document body", messages[-1]["text"])
        self.assertEqual(result["reply"], messages[-1]["text"])

    def test_tool_completion_publishes_artifact_sse_event(self):
        job_id = "artifact-sse-test"
        messages = []
        job = {
            "created_at": time.monotonic(),
            "status": "running",
            "events": [],
            "last_event_id": 0,
            "memory_event_sent": False,
            "final_event_sent": False,
        }
        client = self.web.app.test_client()

        def fake_ask_stream(*_args, **_kwargs):
            tools.create_artifact("SSE artifact", "markdown", "# saved")
            yield {"type": "token", "text": "long artifact body"}
            yield {
                "type": "final",
                "reply": "long artifact body",
                "model": "fake/model",
                "_meta": None,
            }

        with (
            patch.object(self.web, "active_job_id", job_id),
            patch.object(self.web, "current_project", "project-a"),
            patch.object(self.web, "messages", messages),
            patch.object(self.web, "ask_stream", fake_ask_stream),
            patch.object(self.web, "_save_current_chat"),
            patch.object(self.web, "_workspace_file_mtimes", return_value={}),
            patch.object(self.web, "save_history"),
            patch.object(self.web, "load_usage", return_value={}),
            patch.object(self.web, "format_usage", return_value="usage"),
            patch.object(self.web, "schedule_memory_scan"),
            patch.object(self.web, "schedule_thread_summary"),
            patch.dict(self.web.chat_jobs, {job_id: job}),
        ):
            self.web._run_chat_job(job_id, "Create this artifact")
            response = client.get(f"/events/{job_id}")
            event_types = [
                event["data"]["type"]
                for event in self.web.chat_jobs[job_id]["events"]
            ]

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: artifact", body)
        self.assertIn('"artifact_type":"markdown"', body)
        self.assertIn("event: done", body)
        self.assertEqual(len(messages), 2)
        self.assertIn("Artifact: SSE artifact (id: ", messages[-1]["text"])
        self.assertNotIn("long artifact body", messages[-1]["text"])
        self.assertEqual(event_types[0], "artifact")


if __name__ == "__main__":
    unittest.main()
