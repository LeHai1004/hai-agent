import os
from dotenv import load_dotenv
from google import genai
from google.genai import types
from openai import OpenAI
from transfer import export_chat, import_chat, list_exports
from gitcoach import git_coach
from visual import visualize, play
from translate import translate
from config import MODELS
from history import load_history, save_history
from usage import format_usage, load_usage, record_usage
from search import build_prompt, web_search
from llm import ask
from pins import load_pins, add_pin, remove_pin, format_pins
from project_workspace import get_workspace_dir
from pdfread import parse_read_command, read_pdf_page
from img import ask_image
from filechat import ask_file

load_dotenv()

NGUONG_CANH_BAO = 8000  # số token ước lượng của lịch sử, vượt thì cảnh báo


def uoc_luong_token(messages):
    # ước lượng thô: tiếng Việt khoảng 3 ký tự = 1 token
    return sum(len(m["text"]) for m in messages) // 3

def main():
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    groq_api_key = os.environ.get("GROQ_API_KEY")
    openrouter_api_key = os.environ.get("OPENROUTER_API_KEY")

    gemini_client = None
    if gemini_api_key:
        gemini_client = genai.Client(
            api_key=gemini_api_key,
            http_options=types.HttpOptions(api_version="v1beta"),
        )

    openai_clients = {}
    if groq_api_key:
        openai_clients["groq"] = OpenAI(
            api_key=groq_api_key,
            base_url="https://api.groq.com/openai/v1",
        )
    if openrouter_api_key:
        openai_clients["openrouter"] = OpenAI(
            api_key=openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
        )

    if not gemini_client and not openai_clients:
        print("Chưa có API key nào trong file .env (cần ít nhất GEMINI_API_KEY, GROQ_API_KEY hoặc OPENROUTER_API_KEY).")
        return

    messages = load_history()
    pins = load_pins()
    usage = load_usage()
    da_bao = False

    print(f"Chatbot. Đã nạp {len(messages)} tin nhắn cũ.")
    print("Lệnh: /new chat mới | /usage lượt dùng | /web câu hỏi | /en câu dịch | /git owner/repo | /vis nội dung | /play nội dung | /read file.pdf [trang] | /img ảnh câu hỏi | /file đường_dẫn câu hỏi | /pin, /pins, /unpin | /export, /import | exit\n")
    while True:
        text = input("Bạn: ").strip()
        if not text:
            continue
        if text.lower() in ("exit", "quit"):
            break
        if text.startswith("/pin "):
            print(add_pin(pins, text[5:]) + "\n")
            continue
        if text == "/pins":
            print(format_pins(pins) + "\n")
            continue
        if text.startswith("/unpin"):
            print(remove_pin(pins, text[6:].strip()) + "\n")
            continue
        if text == "/new":
            messages = []
            da_bao = False
            save_history(messages)
            print("Đã bắt đầu cuộc chat mới.\n")
            continue
        if text == "/usage":
            print(f"Lượt dùng ngày {usage['date']}:")
            for provider, model in MODELS:
                pm = f"{provider}/{model}"
                print("  " + format_usage(usage, pm))
            print()
            continue

        if text.startswith("/export"):
            json_path, md_path = export_chat(messages, text[7:])
            print(f"Đã xuất: {json_path} và {md_path}\n")
            continue
        if text.startswith("/import"):
            name = text[7:].strip()
            if not name:
                print("Các bản đã xuất: " + (", ".join(list_exports()) or "chưa có") + "\n")
                continue
            data, error = import_chat(name)
            if error:
                print(error + "\n")
                continue
            messages = data
            da_bao = False
            save_history(messages)
            print(f"Đã nạp {len(messages)} tin nhắn từ '{name}'.\n")
            continue
        question = text
        prompt = text
        sources = []
        if text.startswith("/en "):
            sentence = text[4:].strip()
            if not sentence:
                print("Dùng: /en câu cần dịch\n")
                continue
            try:
                reply, model_name, meta = translate(gemini_client, openai_clients, sentence)
            except Exception as e:
                print(f"Lỗi dịch: {e}\n")
                continue
            print(f"\n{reply}\n[{model_name}]\n")
            continue
        if text.startswith("/git "):
            repo_name = text[5:].strip()
            if not repo_name:
                print("Dùng: /git owner/repo (ví dụ: /git torvalds/linux)\n")
                continue
            try:
                reply, model_name, meta = git_coach(gemini_client, openai_clients, repo_name)
            except Exception as e:
                print(f"Lỗi Git Coach: {e}\n")
                continue
            print(f"\n{reply}\n[{model_name}]\n")
            continue
        if text.startswith("/vis "):
            vis_content = text[5:].strip()
            if not vis_content:
                print("Dùng: /vis câu hỏi hoặc đoạn code\n")
                continue
            try:
                reply, model_name, meta = visualize(gemini_client, openai_clients, vis_content)
            except Exception as e:
                print(f"Lỗi trực quan hóa: {e}\n")
                continue
            print(f"\n{reply}\n\n[Đã lưu sơ đồ vào {(get_workspace_dir() / 'vis.md').as_posix()}]\n[{model_name}]\n")
            continue

        if text.startswith("/play "):
            play_content = text[6:].strip()
            if not play_content:
                print("Dùng: /play chủ đề cần giải thích\n")
                continue
            try:
                reply, model_name, meta = play(gemini_client, openai_clients, play_content)
            except Exception as e:
                print(f"Lỗi tạo video: {e}\n")
                continue
            print(f"\n[Đã mở video giải thích trong trình duyệt]\n[{model_name}]\n")
            continue


        if text.startswith("/read "):
            args = text[6:].strip()
            read_command = parse_read_command(args)
            if read_command is None:
                print("Dùng: /read đường_dẫn.pdf [số trang]\n")
                continue
            args, page_no = read_command
            try:
                msg, model_name = read_pdf_page(gemini_client, openai_clients, args, page_no)
            except Exception as e:
                print(f"Lỗi đọc PDF: {e}\n")
                continue
            print(f"\n{msg}\n[{model_name}]\n")
            continue

        if text.startswith("/img "):
            rest = text[5:].strip()
            if rest.startswith('"') and '"' in rest[1:]:
                path, _, question = rest[1:].partition('"')
            else:
                path, _, question = rest.partition(" ")
            path, question = path.strip(), question.strip()
            if not path:
                print("Dùng: /img đường_dẫn_ảnh câu hỏi (ví dụ: /img workspace/loi.png giải thích lỗi này)\n")
                continue
            try:
                reply, model_name, meta = ask_image(gemini_client, openai_clients, path, question)
            except Exception as e:
                print(f"Lỗi đọc ảnh: {e}\n")
                continue
            if not model_name:
                print(reply + "\n")
                continue
            record_usage(usage, model_name, meta)
            print(f"\nBot ({model_name}): {reply}\n")
            continue

        if text.startswith("/file "):
            rest = text[6:].strip()
            if rest.startswith('"') and '"' in rest[1:]:
                path, _, question = rest[1:].partition('"')
            else:
                path, _, question = rest.partition(" ")
            path, question = path.strip(), question.strip()
            if not path:
                print("Dùng: /file đường_dẫn câu hỏi (ví dụ: /file chat.py giải thích hàm main)\n")
                continue
            try:
                reply, model_name, meta, note = ask_file(gemini_client, openai_clients, path, question)
            except Exception as e:
                print(f"Lỗi đọc file: {e}\n")
                continue
            if not model_name:
                print(reply + "\n")
                continue
            record_usage(usage, model_name, meta)
            print(f"\nBot ({model_name}): {reply}\n{note}\n")
            continue

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

        if not sources and not da_bao and uoc_luong_token(messages) > NGUONG_CANH_BAO:
                    print(f"⚠ Lịch sử chat đã khoảng {uoc_luong_token(messages)} token, "
                        "mỗi câu hỏi đều gửi lại toàn bộ chỗ này nên sẽ tốn nhiều.")
                    print("  Gợi ý: gõ /export rồi /new để bắt đầu chat mới (lịch sử vẫn lưu, cần thì /import lại).")
                    da_bao = True
                    if input("  Vẫn gửi câu này? (y/n): ").strip().lower() != "y":
                        print()
                        continue
        try:
            answer, model_key, meta = ask(gemini_client, openai_clients, messages, prompt, use_history=not sources)
            messages.append({"role": "user", "text": question})
            saved = answer
            if sources:
                saved += "\n\nNguồn:\n" + "\n".join(
                    f"[{i}] {t}: {u}" for i, (t, u) in enumerate(sources, 1)
                )
            messages.append({"role": "model", "text": saved})
            save_history(messages)
            record_usage(usage, model_key, meta)
            print(f"Bot ({model_key}): {answer}\n")
            if sources:
                print("Nguồn:")
                for i, (title, uri) in enumerate(sources, 1):
                    print(f"  [{i}] {title}: {uri}")
                print()
            print(f"  [{format_usage(usage, model_key)}]\n")
        except Exception as e:
            print(f"Lỗi: tất cả model đều thất bại. {e}\n")


if __name__ == "__main__":
    main()
