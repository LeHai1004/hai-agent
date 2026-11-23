import os
from llm import ask


def build_vis_prompt(text):
    return (
        "Bạn là chuyên gia kiến trúc hệ thống và luồng dữ liệu.\n"
        "Hãy phân tích câu hỏi hoặc đoạn code sau và trả về bằng tiếng Việt, ngắn gọn, súc tích, luôn tuân thủ đúng 4 phần sau:\n\n"
        "1. Bức tranh toàn cảnh: kể cả quá trình từ lúc bấm/chạy đến lúc ra kết quả, gồm cả phần người hỏi chưa hỏi (biên dịch, JVM, RAM, CPU, ổ đĩa, mạng nếu có). Nếu không chắc thì ghi 'chưa chắc', không bịa.\n"
        "2. Một sơ đồ Mermaid sequenceDiagram minh họa luồng hoạt động:\n"
        "```mermaid\n"
        "sequenceDiagram\n"
        "    ... (vẽ sơ đồ các thành phần)\n"
        "```\n"
        "3. Các bước đánh số chi tiết, mỗi bước ghi rõ dữ liệu đang nằm ở đâu (RAM, ổ đĩa hay mạng).\n"
        "4. 'Có thể bạn chưa biết': 3 câu nên hỏi tiếp, mỗi câu kèm trả lời 1 dòng.\n\n"
        f"Input: {text}"
    )


def save_workspace_vis(content):
    os.makedirs("workspace", exist_ok=True)
    path = os.path.join("workspace", "vis.md")
    # Trích xuất khối mermaid nếu có
    mermaid_code = ""
    if "```mermaid" in content:
        parts = content.split("```mermaid")
        if len(parts) > 1:
            m_part = parts[1].split("```")[0].strip()
            mermaid_code = "```mermaid\n" + m_part + "\n```"
    
    with open(path, "w", encoding="utf-8") as f:
        f.write(mermaid_code if mermaid_code else content)
    return path


def visualize(gemini_client, openai_clients, text):
    prompt = build_vis_prompt(text)
    reply, model_name, meta = ask(gemini_client, openai_clients, [], prompt, use_history=False, route_text=text)
    save_workspace_vis(reply)
    return reply, model_name, meta
