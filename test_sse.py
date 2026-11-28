import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

import chat_flow
import llm


def _chunk(text):
    part = SimpleNamespace(text=text, function_call=None)
    content = SimpleNamespace(parts=[part])
    candidate = SimpleNamespace(content=content, finish_reason="STOP")
    return SimpleNamespace(candidates=[candidate], usage_metadata=None)


def _tool_chunk(name):
    function_call = SimpleNamespace(name=name, args={})
    part = SimpleNamespace(text=None, function_call=function_call)
    content = SimpleNamespace(parts=[part])
    candidate = SimpleNamespace(content=content, finish_reason=None)
    return SimpleNamespace(candidates=[candidate], usage_metadata=None)


class _FakeChat:
    def __init__(self, stream_factory):
        self.stream_factory = stream_factory

    def send_message_stream(self, message):
        self.message = message
        return self.stream_factory()


class _FakeChats:
    def __init__(self, chats):
        self.chats = chats
        self.models = []

    def create(self, model, **kwargs):
        self.models.append(model)
        self.kwargs = kwargs
        return self.chats[model]


class AskStreamTests(unittest.TestCase):
    def test_retries_another_model_if_failure_precedes_first_token(self):
        def fail_before_token():
            raise RuntimeError("before first token")

        client = SimpleNamespace(chats=_FakeChats({
            "first": _FakeChat(fail_before_token),
            "second": _FakeChat(lambda: iter([_chunk("reply")])),
        }))
        with (
            patch.object(
                llm,
                "build_context",
                return_value={"system_extra": "", "messages": []},
            ),
            patch.object(
                llm,
                "available_models",
                return_value=[("gemini", "first"), ("gemini", "second")],
            ),
        ):
            events = list(llm.ask_stream(
                client, {}, [], "prompt", allow_tools=False
            ))

        self.assertEqual(client.chats.models, ["first", "second"])
        self.assertEqual([event["type"] for event in events], ["token", "final"])
        self.assertEqual(events[-1]["model"], "gemini/second")

    def test_does_not_retry_after_first_token_and_keeps_partial_text(self):
        def fail_after_token():
            yield _chunk("partial")
            raise RuntimeError("after first token")

        client = SimpleNamespace(chats=_FakeChats({
            "first": _FakeChat(fail_after_token),
            "second": _FakeChat(lambda: iter([_chunk("must not run")])),
        }))
        with (
            patch.object(
                llm,
                "build_context",
                return_value={"system_extra": "", "messages": []},
            ),
            patch.object(
                llm,
                "available_models",
                return_value=[("gemini", "first"), ("gemini", "second")],
            ),
        ):
            events = list(llm.ask_stream(
                client, {}, [], "prompt", allow_tools=False
            ))

        self.assertEqual(client.chats.models, ["first"])
        self.assertEqual([event["type"] for event in events], ["token", "error"])
        self.assertEqual(events[-1]["partial"], "partial")

    def test_openai_compatible_provider_emits_one_full_reply_token(self):
        with (
            patch.object(
                llm,
                "build_context",
                return_value={"system_extra": "", "messages": []},
            ),
            patch.object(
                llm,
                "available_models",
                return_value=[("groq", "test-model")],
            ),
            patch.object(
                llm,
                "run_openai",
                return_value=("whole reply", 12),
            ),
        ):
            events = list(llm.ask_stream(
                None,
                {"groq": object()},
                [],
                "prompt",
                allow_tools=False,
            ))

        self.assertEqual([event["type"] for event in events], ["token", "final"])
        self.assertEqual(events[0]["text"], "whole reply")
        self.assertEqual(events[-1]["reply"], "whole reply")

    def test_function_call_chunk_emits_tool_status_event(self):
        client = SimpleNamespace(chats=_FakeChats({
            "first": _FakeChat(
                lambda: iter([_tool_chunk("get_current_time"), _chunk("done")])
            ),
        }))
        with (
            patch.object(
                llm,
                "build_context",
                return_value={"system_extra": "", "messages": []},
            ),
            patch.object(
                llm,
                "available_models",
                return_value=[("gemini", "first")],
            ),
        ):
            events = list(llm.ask_stream(client, {}, [], "prompt"))

        self.assertEqual(
            [event["type"] for event in events],
            ["tool", "token", "final"],
        )
        self.assertEqual(events[0]["name"], "get_current_time")


class ChatFlowStreamTests(unittest.TestCase):
    def test_memory_event_is_emitted_only_after_verified_save(self):
        def fake_stream(*args, **kwargs):
            self.assertEqual(args[3], "prompt")
            self.assertIn("memory_saved", kwargs)
            yield {"type": "token", "text": "OK"}
            yield {
                "type": "final",
                "reply": "OK",
                "model": "fake/model",
                "_meta": None,
            }

        def fake_ask(*args, **kwargs):
            self.assertTrue(args or kwargs)
            return "", "", None

        for saved in (True, False):
            with (
                patch.object(chat_flow, "memory_intent", return_value="remember"),
                patch.object(
                    chat_flow,
                    "save_and_verify",
                    return_value={"saved": saved, "error": None},
                ),
            ):
                events = list(chat_flow.run_chat_turn_stream(
                    None,
                    {},
                    [],
                    "prompt",
                    "nhớ một điều",
                    ask_stream_fn=fake_stream,
                    ask_fn=fake_ask,
                    record_usage_fn=lambda *args: args,
                ))

            memory_events = [event for event in events if event["type"] == "memory"]
            self.assertEqual(bool(memory_events), saved)


def _iter_sse(response):
    event_id = None
    data_lines = []
    for line in response.iter_lines(decode_unicode=True):
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if not line:
            if data_lines:
                yield {
                    "id": int(event_id),
                    "data": json.loads("\n".join(data_lines)),
                }
            event_id = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("id:"):
            event_id = line[3:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())


def run_sse_smoke_test():
    original_directory = os.getcwd()
    original_ask_stream = None
    server = None
    server_thread = None
    temporary_directory = tempfile.TemporaryDirectory()
    try:
        os.chdir(temporary_directory.name)
        import web

        original_ask_stream = web.ask_stream

        def fake_ask_stream(*args, **kwargs):
            if kwargs.get("memory_intent") != "ordinary":
                raise AssertionError("unexpected memory intent in test request")
            if "failure" in args[3]:
                yield {"type": "token", "text": "partial"}
                yield {
                    "type": "error",
                    "message": "synthetic failure",
                    "partial": "partial",
                    "model": "fake/model",
                }
                return
            yield {"type": "token", "text": "alpha "}
            time.sleep(0.5)
            yield {"type": "token", "text": "beta"}
            yield {
                "type": "final",
                "reply": "alpha beta",
                "model": "fake/model",
                "_meta": None,
            }

        web.ask_stream = fake_ask_stream
        from werkzeug.serving import make_server

        server = make_server("127.0.0.1", 0, web.app, threaded=True)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        response = requests.post(
            f"{base_url}/api/chat",
            json={"text": "SSE reconnect integration test"},
            timeout=10,
        )
        response.raise_for_status()
        request_id = response.json()["request_id"]
        events_url = f"{base_url}/events/{request_id}"
        started_at = time.perf_counter()
        received = []

        with requests.get(events_url, stream=True, timeout=(5, 15)) as stream:
            stream.raise_for_status()
            first_event = next(_iter_sse(stream))
            received.append(first_event)
            print(
                f"+{time.perf_counter() - started_at:.3f}s "
                f"id={first_event['id']} "
                f"event={first_event['data']['type']} "
                f"data={json.dumps(first_event['data'], ensure_ascii=True)}"
            )

        last_event_id = first_event["id"]
        print(f"disconnect; reconnect with Last-Event-ID: {last_event_id}")
        with requests.get(
            events_url,
            headers={"Last-Event-ID": str(last_event_id)},
            stream=True,
            timeout=(5, 15),
        ) as stream:
            stream.raise_for_status()
            for event in _iter_sse(stream):
                received.append(event)
                print(
                    f"+{time.perf_counter() - started_at:.3f}s "
                    f"id={event['id']} "
                    f"event={event['data']['type']} "
                    f"data={json.dumps(event['data'], ensure_ascii=True)}"
                )

        ids = [event["id"] for event in received]
        event_types = [event["data"]["type"] for event in received]
        if len(ids) != len(set(ids)) or ids != sorted(ids):
            raise AssertionError(f"duplicate or unordered SSE ids: {ids!r}")
        if any(event_id <= last_event_id for event_id in ids[1:]):
            raise AssertionError("reconnected stream repeated an event")
        if event_types[-2:] != ["final", "done"]:
            raise AssertionError(f"unexpected terminal events: {event_types!r}")
        token_text = "".join(
            event["data"]["text"]
            for event in received
            if event["data"]["type"] == "token"
        )
        final_reply = next(
            event["data"]["reply"]
            for event in received
            if event["data"]["type"] == "final"
        )
        if token_text != final_reply:
            raise AssertionError(
                f"tokens {token_text!r} did not match final {final_reply!r}"
            )
        print(f"PASS: received event IDs {ids}; no replay duplicates.")

        history_response = requests.get(f"{base_url}/api/history", timeout=10)
        history_response.raise_for_status()
        history = history_response.json()
        model_messages = [item for item in history if item["role"] == "model"]
        if len(model_messages) != 1 or model_messages[0]["text"] != "alpha beta":
            raise AssertionError(
                f"chat history did not store one complete reply: {history!r}"
            )

        failed_response = requests.post(
            f"{base_url}/api/chat",
            json={"text": "SSE stream failure test"},
            timeout=10,
        )
        failed_response.raise_for_status()
        failed_id = failed_response.json()["request_id"]
        failed_events = []
        failure_started = time.perf_counter()
        with requests.get(
            f"{base_url}/events/{failed_id}",
            stream=True,
            timeout=(5, 15),
        ) as stream:
            stream.raise_for_status()
            for event in _iter_sse(stream):
                failed_events.append(event)
                print(
                    f"+{time.perf_counter() - failure_started:.3f}s "
                    f"id={event['id']} "
                    f"event={event['data']['type']} "
                    f"data={json.dumps(event['data'], ensure_ascii=True)}"
                )
        failed_types = [event["data"]["type"] for event in failed_events]
        if failed_types != ["token", "error"]:
            raise AssertionError(
                f"expected token then terminal error, got {failed_types!r}"
            )
        status_response = requests.get(
            f"{base_url}/api/chat/status/{failed_id}",
            timeout=10,
        )
        status_response.raise_for_status()
        failed_result = status_response.json()["result"]
        if "partial" not in failed_result["reply"] or "synthetic failure" not in failed_result["reply"]:
            raise AssertionError(
                f"legacy poll did not preserve partial/error: {failed_result!r}"
            )
        print("PASS: stream error is terminal; polling retains partial text and error.")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5)
        if original_ask_stream is not None:
            web.ask_stream = original_ask_stream
        os.chdir(original_directory)
        temporary_directory.cleanup()


def main():
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(AskStreamTests))
    suite.addTests(
        unittest.defaultTestLoader.loadTestsFromTestCase(ChatFlowStreamTests)
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1
    run_sse_smoke_test()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
