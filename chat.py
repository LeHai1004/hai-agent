import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

MODEL = "gemini-3.5-flash"


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Chưa có GEMINI_API_KEY (kiểm tra file .env).")
        return

    client = genai.Client(api_key=api_key)
    chat = client.chats.create(model=MODEL)

    print(f"Chatbot ({MODEL}). Gõ 'exit' để thoát.\n")
    while True:
        text = input("Bạn: ").strip()
        if not text:
            continue
        if text.lower() in ("exit", "quit"):
            break
        try:
            reply = chat.send_message(text)
            print(f"Bot: {reply.text}\n")
        except Exception as e:
            print(f"Lỗi: {e}\n")


if __name__ == "__main__":
    main()