import json
import re
import threading

from config import MAX_THREAD_MESSAGES, MEMORY_BACKGROUND_SCAN
from memory import (
    InvalidModelJSON,
    build_deletion_prompt,
    delete_and_verify,
    list_memories,
    memory_intent,
    parse_delete_ids,
    memory_log,
    request_json_model,
    scan_recent_memories,
    save_and_verify,
)


_REMEMBER_PREFIX_RE = re.compile(
    r"^\s*(?:(?:hãy\s+)?nhớ(?:\s+giúp)?"
    r"(?:\s+(?:tôi|mình|em))?(?:\s+(?:rằng|là))?|"
    r"ghi\s+nhớ|remember)\s*",
    re.IGNORECASE,
)
_SAVE_CLAIM_RE = re.compile(
    r"\b(?:đã\s+(?:lưu|ghi\s+nhận|ghi\s+nhớ|nhớ|cập\s+nhật)|"
    r"sẽ\s+(?:lưu|nhớ|ghi\s+nhớ)|ghi\s+nhớ\s+rồi|lưu\s+(?:lại\s+)?thành\s+công)\b",
    re.IGNORECASE,
)
_DELETE_CLAIM_RE = re.compile(
    r"\b(?:đã\s+(?:quên|xóa|xoá|gỡ|loại\s+bỏ)|"
    r"(?:xóa|xoá)\s+xong|đã\s+xóa\s+khỏi\s+bộ\s+nhớ)\b",
    re.IGNORECASE,
)
_SUMMARY_JOBS_LOCK = threading.Lock()
_SUMMARY_JOBS = set()


def _remember_text(text):
    text = _REMEMBER_PREFIX_RE.sub("", text, count=1)
    return re.sub(r"^[\s:,\-–—]+", "", text).strip()


def _forget_request(
    gemini_client,
    openai_clients,
    text,
    ask_fn,
    record_usage_fn,
):
    existing = list_memories()
    if not existing:
        return {
            "intent": "forget",
            "deleted": [],
            "error": "Không có mục bộ nhớ để xóa.",
        }
    try:
        data, _, _ = request_json_model(
            gemini_client,
            openai_clients,
            [],
            build_deletion_prompt(text, existing),
            ask_fn,
            record_usage_fn,
            route_text="chọn mục bộ nhớ cần xóa",
        )
    except InvalidModelJSON:
        memory_log("[memory] bỏ xóa: model trả JSON không hợp lệ")
        return {
            "intent": "forget",
            "deleted": [],
            "error": "Không đọc được lựa chọn xóa từ model.",
        }
    except Exception as error:
        return {
            "intent": "forget",
            "deleted": [],
            "error": f"Không thể xác định mục cần xóa: {error}",
        }

    delete_ids = parse_delete_ids(data, existing)
    if delete_ids is None:
        return {
            "intent": "forget",
            "deleted": [],
            "error": "Không đọc được lựa chọn xóa từ model.",
        }
    if not delete_ids:
        return {
            "intent": "forget",
            "deleted": [],
            "error": "Không tìm thấy mục phù hợp để xóa.",
        }

    existing_by_id = {item["id"]: item for item in existing}
    deleted = []
    errors = []
    for memory_id in delete_ids:
        result = delete_and_verify(memory_id)
        if result["deleted"]:
            deleted.append(existing_by_id[memory_id])
        elif result["error"]:
            errors.append(result["error"])
    return {
        "intent": "forget",
        "deleted": deleted,
        "error": "; ".join(errors) if errors else None,
    }


def run_chat_turn(
    gemini_client,
    openai_clients,
    messages,
    prompt,
    user_text,
    ask_fn,
    record_usage_fn,
    use_history=True,
    project=None,
    summary="",
    summary_upto=0,
):
    intent = memory_intent(user_text)
    memory_result = None
    memory_saved = None
    if intent == "remember":
        remembered_text = _remember_text(user_text)
        memory_result = save_and_verify(remembered_text)
        memory_result["intent"] = "remember"
        memory_result["text"] = remembered_text
        memory_saved = memory_result["saved"]
    elif intent == "forget":
        memory_result = _forget_request(
            gemini_client,
            openai_clients,
            user_text,
            ask_fn,
            record_usage_fn,
        )

    answer, model_key, meta = ask_fn(
        gemini_client,
        openai_clients,
        messages,
        prompt,
        use_history=use_history,
        project=project,
        memory_saved=memory_saved,
        summary=summary,
        summary_upto=summary_upto,
        memory_intent=intent or "ordinary",
        memory_forget_failed=bool(
            intent == "forget" and memory_result and memory_result.get("error")
        ),
    )
    if intent == "remember" and memory_saved is False and _SAVE_CLAIM_RE.search(answer or ""):
        error = memory_result["error"] or "Không xác minh được việc lưu."
        answer = f"Lưu thất bại, chưa lưu được thông tin: {error}"
    if intent == "forget" and _DELETE_CLAIM_RE.search(answer or ""):
        answer = "Mình sẽ không khẳng định đã xóa bộ nhớ trong câu trả lời."
    return answer, model_key, meta, memory_result


def run_chat_turn_stream(
    gemini_client,
    openai_clients,
    messages,
    prompt,
    user_text,
    ask_stream_fn,
    ask_fn,
    record_usage_fn,
    use_history=True,
    project=None,
    summary="",
    summary_upto=0,
):
    intent = memory_intent(user_text)
    memory_result = None
    memory_saved = None
    if intent == "remember":
        remembered_text = _remember_text(user_text)
        memory_result = save_and_verify(remembered_text)
        memory_result["intent"] = "remember"
        memory_result["text"] = remembered_text
        memory_saved = memory_result["saved"]
        if memory_saved:
            yield {"type": "memory", "text": remembered_text}
    elif intent == "forget":
        memory_result = _forget_request(
            gemini_client,
            openai_clients,
            user_text,
            ask_fn,
            record_usage_fn,
        )

    streamed_text = []
    for event in ask_stream_fn(
        gemini_client,
        openai_clients,
        messages,
        prompt,
        use_history=use_history,
        project=project,
        memory_saved=memory_saved,
        summary=summary,
        summary_upto=summary_upto,
        memory_intent=intent or "ordinary",
        memory_forget_failed=bool(
            intent == "forget" and memory_result and memory_result.get("error")
        ),
    ):
        if event["type"] == "token":
            streamed_text.append(event.get("text", ""))
            yield event
        elif event["type"] == "final":
            answer = event["reply"]
            if (
                intent == "remember"
                and memory_saved is False
                and _SAVE_CLAIM_RE.search(answer or "")
            ):
                error = memory_result["error"] or "Không xác minh được việc lưu."
                answer = f"Lưu thất bại, chưa lưu được thông tin: {error}"
            if intent == "forget" and _DELETE_CLAIM_RE.search(answer or ""):
                answer = "Mình sẽ không khẳng định đã xóa bộ nhớ trong câu trả lời."
            yield {
                **event,
                "reply": answer,
                "memory_result": memory_result,
            }
        elif event["type"] == "error":
            yield {
                **event,
                "partial": event.get("partial", "".join(streamed_text)),
                "memory_result": memory_result,
            }
        else:
            yield event


def schedule_memory_scan(
    gemini_client,
    openai_clients,
    messages,
    ask_fn,
    record_usage_fn,
    on_saved=None,
):
    if not MEMORY_BACKGROUND_SCAN:
        return None
    user_turns = sum(
        1
        for item in messages
        if isinstance(item, dict) and item.get("role") == "user"
    )
    if not user_turns or user_turns % 3:
        return None
    recent_messages = list(messages[-6:])
    try:
        worker = threading.Thread(
            target=scan_recent_memories,
            args=(
                gemini_client,
                openai_clients,
                recent_messages,
                ask_fn,
                record_usage_fn,
                on_saved,
            ),
            daemon=True,
        )
        worker.start()
    except Exception as error:
        memory_log(f"[memory] lỗi khởi chạy quét nền: {error}")
        return None
    return worker


def schedule_thread_summary(
    gemini_client,
    openai_clients,
    messages,
    summary,
    summary_upto,
    ask_fn,
    record_usage_fn,
    persist_fn,
    chat_key=None,
):
    if len(messages) <= MAX_THREAD_MESSAGES:
        return None
    target_upto = len(messages) - 8
    if target_upto <= summary_upto:
        return None
    key = chat_key if chat_key is not None else "default"
    with _SUMMARY_JOBS_LOCK:
        if key in _SUMMARY_JOBS:
            return None
        _SUMMARY_JOBS.add(key)

    old_messages = list(messages[summary_upto:target_upto])

    def summarize():
        try:
            prompt = (
                "Gộp phần tóm tắt cũ với các tin nhắn mới đã cũ. "
                "Giữ các thông tin cần thiết cho hội thoại tiếp tục, không bịa, "
                "không vượt quá khoảng 150 token. Chỉ trả phần tóm tắt.\n"
                f"Tóm tắt cũ:\n{summary}\n"
                "Các tin nhắn mới cần gộp:\n"
                f"{json.dumps(old_messages, ensure_ascii=False)}"
            )
            text, model_key, meta = ask_fn(
                gemini_client,
                openai_clients,
                [],
                prompt,
                use_history=False,
                route_text="tóm tắt hội thoại",
                model_override="gemini/gemini-3.5-flash-lite",
                include_persistent_context=False,
                allow_tools=False,
            )
            record_usage_fn(model_key, meta)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Model trả về tóm tắt rỗng hoặc không phải văn bản.")
            compact_summary = " ".join(text.split())[:450].rstrip()
            persist_fn(compact_summary, target_upto)
            memory_log(f"[memory] đã cập nhật tóm tắt chat đến tin {target_upto}")
        except Exception as error:
            memory_log(f"[memory] lỗi tóm tắt chat: {error}")
        finally:
            with _SUMMARY_JOBS_LOCK:
                _SUMMARY_JOBS.discard(key)

    try:
        worker = threading.Thread(target=summarize, daemon=True)
        worker.start()
    except Exception as error:
        with _SUMMARY_JOBS_LOCK:
            _SUMMARY_JOBS.discard(key)
        memory_log(f"[memory] lỗi khởi chạy tóm tắt chat: {error}")
        return None
    return worker
