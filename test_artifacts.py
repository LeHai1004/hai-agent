import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import artifacts
import tools
import tools.files


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

    def test_artifact_view_renders_markdown_and_code_with_project_scoping(self):
        markdown = artifacts.create_artifact(
            "Guide <safe>",
            "markdown",
            "# Git guide\n\n**bold**\n</textarea><script>alert(1)</script>",
            project="project-a",
        )
        code = artifacts.create_artifact(
            "Prime check",
            "code",
            "print('<safe>')",
            language="python",
            project="project-a",
        )
        other_project = artifacts.create_artifact(
            "Private",
            "markdown",
            "private",
            project="project-b",
        )
        client = self.web.app.test_client()

        with patch.object(self.web, "current_project", "project-a"):
            markdown_page = client.get(f"/artifact-view/{markdown['id']}")
            self.assertEqual(markdown_page.status_code, 200)
            markdown_html = markdown_page.get_data(as_text=True)
            self.assertIn("Guide &lt;safe&gt;", markdown_html)
            self.assertIn("render(document.getElementById('artifact-source').value)", markdown_html)
            self.assertIn("&lt;/textarea&gt;&lt;script&gt;", markdown_html)
            self.assertIn("Sao chép", markdown_html)
            self.assertIn("Tải về", markdown_html)

            code_page = client.get(f"/artifact-view/{code['id']}")
            self.assertEqual(code_page.status_code, 200)
            code_html = code_page.get_data(as_text=True)
            self.assertIn("class=\"language-label\">python", code_html)
            self.assertIn("print('&lt;safe&gt;')", code_html)
            self.assertIn("python:'py'", code_html)
            self.assertEqual(
                client.get(f"/artifact-view/{other_project['id']}").status_code,
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
            job_events = list(self.web.chat_jobs[job_id]["events"])
            event_types = [
                event["data"]["type"]
                for event in job_events
            ]

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: artifact", body)
        self.assertIn('"artifact_type":"markdown"', body)
        self.assertIn("event: done", body)
        self.assertEqual(len(messages), 2)
        self.assertIn("Artifact: SSE artifact (id: ", messages[-1]["text"])
        self.assertNotIn("long artifact body", messages[-1]["text"])
        artifact_events = [
            event["data"]
            for event in job_events
            if event["data"]["type"] == "artifact"
        ]
        self.assertEqual(len(artifact_events), 1)
        artifact_id = artifact_events[0]["id"]
        self.assertEqual(
            messages[-1]["text"],
            f"Artifact: SSE artifact (id: {artifact_id})",
        )
        self.assertEqual(event_types[0], "artifact")

    def test_live_artifact_renderer_replaces_and_deduplicates_cards(self):
        renderer = self.web.PAGE.split(
            "function renderBotReply(container, text){", 1
        )[1].split("function addMsg(", 1)[0]
        self.assertIn("container.replaceChildren();", renderer)
        self.assertIn("const seenIds = new Set();", renderer)
        self.assertIn("if (seenIds.has(reference.id)) return;", renderer)

    def test_created_file_link_is_saved_with_final_chat_reply(self):
        job_id = "created-file-history-test"
        original_reply = "Đã tạo file Word 'guide.docx'."
        expected_link = "📄 File đã tạo: guide.docx — tải về: /files/guide.docx"
        expected_reply = original_reply + "\n\n" + expected_link
        messages = []
        job = {
            "created_at": time.monotonic(),
            "status": "running",
            "events": [],
            "last_event_id": 0,
            "memory_event_sent": False,
            "final_event_sent": False,
        }

        def fake_process(*_args, **_kwargs):
            messages.extend([
                {"role": "user", "text": "Tạo file Word"},
                {"role": "model", "text": original_reply},
            ])
            return {"reply": original_reply, "model": "fake/model"}

        client = self.web.app.test_client()
        with (
            patch.object(self.web, "active_job_id", job_id),
            patch.object(self.web, "messages", messages),
            patch.object(self.web, "_process_chat_message", fake_process),
            patch.object(
                self.web,
                "_workspace_file_mtimes",
                side_effect=[{}, {"guide.docx": 1}],
            ),
            patch.object(self.web, "_save_current_chat"),
            patch.object(self.web, "save_history") as save_history,
            patch.object(self.web, "schedule_memory_scan"),
            patch.object(self.web, "schedule_thread_summary"),
            patch.dict(self.web.chat_jobs, {job_id: job}),
        ):
            self.web._run_chat_job(job_id, "Tạo file Word")
            response = client.get(f"/events/{job_id}")
            completed_result = dict(self.web.chat_jobs[job_id]["result"])
            final_events = [
                event["data"]
                for event in self.web.chat_jobs[job_id]["events"]
                if event["data"]["type"] == "final"
            ]

        self.assertEqual(messages[-1]["text"], expected_reply)
        self.assertEqual(completed_result["reply"], expected_reply)
        self.assertEqual(final_events[0]["reply"], expected_reply)
        self.assertIn(expected_link, response.get_data(as_text=True))
        self.assertEqual(save_history.call_args.args[0], messages)

    def test_confirmation_id_renders_once_and_approves_write_file_once(self):
        self.assertIn("const confirmationCards = new Map();", self.web.PAGE)
        self.assertIn(
            "const confirmationKey = `${requestId}:${confirmation.id}`;",
            self.web.PAGE,
        )
        self.assertIn("if (existing) return existing;", self.web.PAGE)

        class FakeTarget:
            def __init__(self):
                self.parent = self
                self.writes = []

            def exists(self):
                return False

            def mkdir(self, **_kwargs):
                return None

            def write_text(self, content, encoding):
                self.writes.append((content, encoding))

        job_id = "single-write-confirmation-test"
        job = {
            "status": "running",
            "confirmation": None,
            "confirmation_id": 0,
            "deadline": None,
            "decision": None,
            "decision_event": threading.Event(),
        }
        target = FakeTarget()
        results = []
        errors = []
        client = self.web.app.test_client()

        def run_write_file():
            self.web.confirm_context.job_id = job_id
            try:
                results.append(tools.files.write_file("note.txt", "contents"))
            except Exception as error:
                errors.append(error)
            finally:
                del self.web.confirm_context.job_id

        with (
            patch.dict(self.web.chat_jobs, {job_id: job}),
            patch.object(
                tools,
                "CONFIRM_HANDLER",
                self.web._web_confirm_handler,
            ),
            patch.object(
                tools.files,
                "_get_safe_path",
                return_value=(target, None),
            ),
            patch.object(
                tools.files,
                "print",
                lambda *_args, **_kwargs: None,
                create=True,
            ),
        ):
            worker = threading.Thread(target=run_write_file)
            worker.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with self.web.chat_jobs_lock:
                    if job["status"] == "confirmation":
                        confirmation_id = job["confirmation"]["id"]
                        break
                time.sleep(0.005)
            else:
                self.fail(
                    f"write_file did not request confirmation: {errors!r}"
                )

            payload = {
                "request_id": job_id,
                "confirmation_id": confirmation_id,
                "approved": True,
            }
            approved = client.post("/confirm", json=payload)
            duplicate = client.post("/confirm", json=payload)
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(errors, [])
        self.assertEqual(results, ["Đã ghi file 'note.txt' thành công."])
        self.assertEqual(target.writes, [("contents", "utf-8")])


if __name__ == "__main__":
    unittest.main()
