import mimetypes
import os
from llm import ask

MAX_BYTES = 4 * 1024 * 1024  # ảnh lớn quá thì tốn token và dễ lỗi


def load_image(path):
    if not os.path.exists(path):
        return None, f"Không thấy file: {path}"
    mime = mimetypes.guess_type(path)[0] or ""
    if mime not in ("image/png", "image/jpeg", "image/webp"):
        return None, "Chỉ nhận ảnh png, jpg, webp."
    if os.path.getsize(path) > MAX_BYTES:
        return None, "Ảnh lớn hơn 4 MB, hãy thu nhỏ trước."
    with open(path, "rb") as f:
        return (f.read(), mime), None


def ask_image(gemini_client, openai_clients, path, question):
    image, error = load_image(path)
    if error:
        return error, "", None
    question = question or "Mô tả và giải thích nội dung ảnh này."
    return ask(gemini_client, openai_clients, [], question, use_history=False, image=image)