import re

from memory import (
    build_deletion_prompt,
    delete_and_verify,
    list_memories,
    memory_intent,
    parse_delete_ids,
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


def _remember_text(text):
    return _REMEMBER_PREFIX_RE.sub("", text, count=1).strip()


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
        reply, model_key, meta = ask_fn(
            gemini_client,
            openai_clients,
            [],
            build_deletion_prompt(text, existing),
            use_history=False,
            route_text="chọn mục bộ nhớ cần xóa",
            model_override="gemini/gemini-3.5-flash-lite",
            include_persistent_context=False,
            allow_tools=False,
        )
        record_usage_fn(model_key, meta)
    except Exception as error:
        return {
            "intent": "forget",
            "deleted": [],
            "error": f"Không thể xác định mục cần xóa: {error}",
        }

    delete_ids = parse_delete_ids(reply, existing)
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
    )
    if intent == "remember" and memory_saved is False and _SAVE_CLAIM_RE.search(answer or ""):
        error = memory_result["error"] or "Không xác minh được việc lưu."
        answer = f"Lưu thất bại, chưa lưu được thông tin: {error}"
    if intent == "forget" and _DELETE_CLAIM_RE.search(answer or ""):
        answer = "Mình sẽ không khẳng định đã xóa bộ nhớ trong câu trả lời."
    return answer, model_key, meta, memory_result
