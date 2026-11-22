from pathlib import Path

# Thử lần lượt từ trên xuống, model nào lỗi thì chuyển sang model kế
MODELS = ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-3.5-flash-lite"]

# Hạn mức lượt/ngày của từng model (điền số thật xem trong Google AI Studio).
# Ví dụ: {"gemini-3.5-flash": 250}. Để trống thì chỉ hiện số đếm.
LIMITS = {
    "gemini-3.5-flash": 20,
    "gemini-2.5-flash": 20,
    "gemini-3.5-flash-lite": 500,
}

HISTORY_FILE = Path("history") / "current.json"
USAGE_FILE = Path("history") / "usage.json"
