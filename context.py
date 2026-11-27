from config import MAX_THREAD_MESSAGES
from memory import build_memory_context, load_memories
from pins import load_pins, pins_as_context


_GLOBAL_TOKEN_BUDGET = 200
_PROJECT_TOKEN_BUDGET = 300


def _token_count(text):
    return len(text) // 3


def global_layer():
    instruction = (
        "\nChỉ dùng thông tin này khi liên quan câu hỏi; không nhắc lại nếu không cần "
        "và không suy diễn thêm ngoài danh sách."
    )
    memory_budget = _GLOBAL_TOKEN_BUDGET * 3 - len(instruction)
    memory_context = build_memory_context(
        load_memories(),
        max_chars=memory_budget,
    )
    return memory_context + instruction if memory_context else ""


def project_layer(project):
    context = pins_as_context(load_pins(project))
    return context[:_PROJECT_TOKEN_BUDGET * 3]


def thread_layer(messages):
    return list(messages[-MAX_THREAD_MESSAGES:])


def build_context(project, messages):
    global_context = global_layer()
    project_context = project_layer(project)
    thread_messages = thread_layer(messages)
    system_extra = "\n\n".join(
        layer for layer in (global_context, project_context) if layer
    )
    thread_chars = sum(
        len(message.get("text", ""))
        for message in thread_messages
        if isinstance(message, dict) and isinstance(message.get("text"), str)
    )
    return {
        "system_extra": system_extra,
        "messages": thread_messages,
        "stats": {
            "global_tokens": _token_count(global_context),
            "project_tokens": _token_count(project_context),
            "thread_tokens": thread_chars // 3,
        },
    }
