import json
import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# Thử lần lượt từ trên xuống, model nào lỗi thì chuyển sang model kế
MODELS = ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite"]
HISTORY_FILE = Path("history") / "current.json"


def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return []


def save_history(messages):
    HISTORY_FILE.parent.mkdir(exist_ok=True)
    HISTORY_FILE.write_text(
        json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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
            return reply.text, model
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

    print(f"Chatbot. Đã nạp {len(messages)} tin nhắn cũ.")
    print("Gõ '/new' để cuộc chat mới, 'exit' để thoát.\n")
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
        try:
            answer, model = ask(client, messages, text)
            messages.append({"role": "user", "text": text})
            messages.append({"role": "model", "text": answer})
            save_history(messages)
            print(f"Bot ({model}): {answer}\n")
        except Exception as e:
            print(f"Lỗi: tất cả model đều thất bại. {e}\n")


if __name__ == "__main__":
    main()