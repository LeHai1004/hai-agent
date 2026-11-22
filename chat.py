import json
import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

MODEL = "gemini-3.5-flash"
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


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Chưa có GEMINI_API_KEY (kiểm tra file .env).")
        return

    client = genai.Client(api_key=api_key)
    messages = load_history()
    chat = client.chats.create(model=MODEL, history=to_contents(messages))

    print(f"Chatbot ({MODEL}). Đã nạp {len(messages)} tin nhắn cũ.")
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
            chat = client.chats.create(model=MODEL)
            print("Đã bắt đầu cuộc chat mới.\n")
            continue
        try:
            reply = chat.send_message(text)
            messages.append({"role": "user", "text": text})
            messages.append({"role": "model", "text": reply.text})
            save_history(messages)
            print(f"Bot: {reply.text}\n")
        except Exception as e:
            print(f"Lỗi: {e}\n")


if __name__ == "__main__":
    main()