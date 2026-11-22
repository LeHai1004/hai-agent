import json
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# Thử lần lượt từ trên xuống, model nào lỗi thì chuyển sang model kế
MODELS = ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite"]

# Hạn mức lượt/ngày của từng model. Tự điền số thật xem trong Google AI Studio,
# ví dụ: {"gemini-3.5-flash": 250}. Để trống thì chỉ hiện số đếm, không hiện %.
LIMITS = {}

HISTORY_FILE = Path("history") / "current.json"
USAGE_FILE = Path("history") / "usage.json"


def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return []


def save_history(messages):
    HISTORY_FILE.parent.mkdir(exist_ok=True)
    HISTORY_FILE.write_text(
        json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_usage():
    today = str(date.today())
    if USAGE_FILE.exists():
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        if data.get("date") == today:
            return data
    return {"date": today, "models": {}}


def save_usage(usage):
    USAGE_FILE.parent.mkdir(exist_ok=True)
    USAGE_FILE.write_text(
        json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def record_usage(usage, model, meta):
    entry = usage["models"].setdefault(model, {"requests": 0, "tokens": 0})
    entry["requests"] += 1
    if meta and meta.total_token_count:
        entry["tokens"] += meta.total_token_count
    save_usage(usage)


def format_usage(usage, model):
    entry = usage["models"].get(model, {"requests": 0, "tokens": 0})
    text = f"{model}: {entry['requests']} lượt, {entry['tokens']} token hôm nay"
    limit = LIMITS.get(model)
    if limit:
        percent = entry["requests"] * 100 // limit
        text += f" ({entry['requests']}/{limit} lượt, {percent}%)"
    return text


def to_contents(messages):
    return [
        types.Content(role=m["role"], parts=[types.Part(text=m["text"])])
        for m in messages
    ]


def ask(client, messages, text):
    last_error = None
    for model in MODELS:
        try:
            chat = client.chats.create(model=model, history=to_contents(messages))
            reply = chat.send_message(text)
            return reply.text, model, reply.usage_metadata
        except Exception as e:
            last_error = e
            print(f"  [{model}] lỗi, thử model khác: {str(e)[:120]}")
    raise last_error


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Chưa có GEMINI_API_KEY (kiểm tra file .env).")
        return

    client = genai.Client(api_key=api_key)
    messages = load_history()
    usage = load_usage()

    print(f"Chatbot. Đã nạp {len(messages)} tin nhắn cũ.")
    print("Lệnh: '/new' chat mới, '/usage' xem lượt dùng, 'exit' thoát.\n")
    while True:
        text = input("Bạn: ").strip()
        if not text:
            continue
        if text.lower() in ("exit", "quit"):
            break
        if text == "/new":
            messages = []
            save_history(messages)
            print("Đã bắt đầu cuộc chat mới.\n")
            continue
        if text == "/usage":
            print(f"Lượt dùng ngày {usage['date']}:")
            for m in MODELS:
                print("  " + format_usage(usage, m))
            print()
            continue
        try:
            answer, model, meta = ask(client, messages, text)
            messages.append({"role": "user", "text": text})
            messages.append({"role": "model", "text": answer})
            save_history(messages)
            record_usage(usage, model, meta)
            print(f"Bot ({model}): {answer}\n")
            print(f"  [{format_usage(usage, model)}]\n")
        except Exception as e:
            print(f"Lỗi: tất cả model đều thất bại. {e}\n")


if __name__ == "__main__":
    main()