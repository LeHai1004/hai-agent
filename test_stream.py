import os
import sys
import time
from functools import wraps

from dotenv import load_dotenv
from google import genai
from google.genai import types

from llm import SYSTEM_INSTRUCTION, available_models
from tools import TOOLS
from tools.time_tool import get_current_time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="backslashreplace")


def describe_chunk(chunk):
    details = []
    for candidate in getattr(chunk, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                details.append(f"text={text!r}")
            function_call = getattr(part, "function_call", None)
            if function_call and getattr(function_call, "name", None):
                details.append(
                    f"function_call={function_call.name}({function_call.args!r})"
                )
            function_response = getattr(part, "function_response", None)
            if function_response and getattr(function_response, "name", None):
                details.append(
                    f"function_response={function_response.name}"
                )
        finish_reason = getattr(candidate, "finish_reason", None)
        if finish_reason:
            details.append(f"finish_reason={finish_reason}")
    return " | ".join(details) if details else "(chunk không có text/function call)"


def stream_message(chat, label, message):
    print(f"\n=== {label} ===")
    started_at = time.perf_counter()
    first_chunk = True
    try:
        for number, chunk in enumerate(chat.send_message_stream(message), start=1):
            elapsed = time.perf_counter() - started_at
            if first_chunk:
                print(f"Chunk đầu tiên sau {elapsed:.3f}s")
                first_chunk = False
            print(f"[{elapsed:.3f}s] chunk {number}: {describe_chunk(chunk)}")
        if first_chunk:
            print("Stream kết thúc mà không nhận chunk nào.")
        return True
    except Exception as error:
        print(f"LỖI: {type(error).__name__}: {error}")
        return False


def print_history(chat):
    history = chat.get_history()
    print(f"\nLịch sử sau 3 lượt: {len(history)} entries")
    for index, entry in enumerate(history, start=1):
        parts = []
        for part in getattr(entry, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                parts.append(f"text={text[:180]!r}")
            function_call = getattr(part, "function_call", None)
            if function_call and getattr(function_call, "name", None):
                parts.append(f"function_call={function_call.name}")
            function_response = getattr(part, "function_response", None)
            if function_response and getattr(function_response, "name", None):
                parts.append(f"function_response={function_response.name}")
        print(f"  {index}. role={entry.role}: {' | '.join(parts)}")


def main():
    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Thiếu GEMINI_API_KEY; không thể chạy kiểm thử Gemini.")
        return 2

    model_candidates = available_models("giải thích TCP trong 3 câu")
    model = next(
        (name for provider, name in model_candidates if provider == "gemini"),
        None,
    )
    if model is None:
        print(f"Không có model Gemini trong danh sách: {model_candidates!r}")
        return 2

    tool_calls = []

    @wraps(get_current_time)
    def tracked_get_current_time():
        tool_calls.append("get_current_time")
        return get_current_time()

    test_tools = [
        tracked_get_current_time if tool.__name__ == "get_current_time" else tool
        for tool in TOOLS
    ]
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(api_version="v1beta"),
    )
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        tools=test_tools,
    )
    chat = client.chats.create(model=model, history=[], config=config)
    print(f"Model: gemini/{model}")
    print(f"Candidates theo available_models(): {model_candidates!r}")
    print("Cùng một chat object được dùng cho cả 3 lượt.")

    stream_message(
        chat,
        "1. TCP — stream từng chunk",
        "giải thích TCP trong 3 câu",
    )

    tool_calls.clear()
    tool_stream_ok = stream_message(
        chat,
        "2. Yêu cầu gọi get_current_time",
        "Hãy gọi tool get_current_time và cho biết thời gian hiện tại.",
    )
    if tool_calls:
        print(f"Tool được gọi tự động: {tool_calls!r}")
    elif tool_stream_ok:
        print("Tool không được gọi tự động trong lượt này.")
    else:
        print("Không xác định được việc gọi tool vì stream lỗi.")

    stream_message(
        chat,
        "3. Hỏi tiếp để kiểm tra lịch sử",
        "Trong một câu, nhắc lại điểm cốt lõi trong câu trả lời đầu tiên về TCP.",
    )
    print_history(chat)
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
