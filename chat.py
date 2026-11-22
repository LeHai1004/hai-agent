import time
import json
import os
from datetime import date
from pathlib import Path

from ddgs import DDGS
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# Thử lần lượt từ trên xuống, model nào lỗi thì chuyển sang model kế
MODELS = ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-3.5-flash-lite"]

# Hạn mức lượt/ngày của từng model (điền số thật xem trong Google AI Studio).
# Ví dụ: {"gemini-3.5-flash": 250}. Để trống thì chỉ hiện số đếm.
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


def web_search(query, n=5):
    last_error = None
    for _ in range(3):
        try:
            return list(DDGS(timeout=15).text(query, max_results=n))
        except Exception as e:
            last_error = e
            time.sleep(1)
    raise last_error

def build_prompt(question, results):
    blocks = []
    for i, r in enumerate(results, 1):
        blocks.append(
            f"[{i}] {r.get('title', '')}\n{r.get('body', '')}\n"
            f"Nguồn: {r.get('href', '')}"
        )
    context = "\n\n".join(blocks)
    return (
        "Trả lời câu hỏi CHỈ dựa trên kết quả tìm kiếm bên dưới, ghi số [n] "
        "khi dùng thông tin nào. Nếu kết quả không đủ thì nói rõ là không biết, "
        "không tự bịa.\n\n"
        f"Kết quả tìm kiếm:\n{context}\n\nCâu hỏi: {question}"
    )

def ask(client, messages, text, use_history=True):
    history = to_contents(messages) if use_history else []
    last_error = None
    for model in MODELS:
        try:
            chat = client.chats.create(model=model, history=history)
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