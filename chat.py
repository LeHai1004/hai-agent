import os
from dotenv import load_dotenv
from google import genai

from config import MODELS
from history import load_history, save_history
from usage import format_usage, load_usage, record_usage
from search import build_prompt, web_search
from llm import ask

load_dotenv()


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Chưa có GEMINI_API_KEY (kiểm tra file .env).")
        return

    client = genai.Client(api_key=api_key)
    messages = load_history()
    usage = load_usage()

    print(f"Chatbot. Đã nạp {len(messages)} tin nhắn cũ.")
    print("Lệnh: '/new' chat mới, '/usage' xem lượt dùng, '/web câu hỏi' tìm web, 'exit' thoát.\n")
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

        question = text
        prompt = text
        sources = []
        if text.startswith("/web "):
            question = text[5:].strip()
            try:
                results = web_search(question)
            except Exception as e:
                print(f"Lỗi tìm web: {e}\n")
                continue
            if not results:
                print("Không tìm thấy kết quả nào.\n")
                continue
            prompt = build_prompt(question, results)
            sources = [(r.get("title", ""), r.get("href", "")) for r in results]

        try:
            answer, model, meta = ask(client, messages, prompt, use_history=not sources)
            messages.append({"role": "user", "text": question})
            saved = answer
            if sources:
                saved += "\n\nNguồn:\n" + "\n".join(
                    f"[{i}] {t}: {u}" for i, (t, u) in enumerate(sources, 1)
                )
            messages.append({"role": "model", "text": saved})
            save_history(messages)
            record_usage(usage, model, meta)
            print(f"Bot ({model}): {answer}\n")
            if sources:
                print("Nguồn:")
                for i, (title, uri) in enumerate(sources, 1):
                    print(f"  [{i}] {title}: {uri}")
                print()
            print(f"  [{format_usage(usage, model)}]\n")
        except Exception as e:
            print(f"Lỗi: tất cả model đều thất bại. {e}\n")


if __name__ == "__main__":
    main()
