import time
from ddgs import DDGS


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
