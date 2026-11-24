from llm import score_question

for q in [
    "xin chào",
    "decode là gì",
    "giải thích lỗi này giúp mình",
    "so sánh TCP và UDP, tại sao UDP nhanh hơn",
    "```python\ndef f(x): return x\n``` sửa bug",
]:
    print(score_question(q), "|", q[:40])