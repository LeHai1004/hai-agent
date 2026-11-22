from pathlib import Path

# Thử lần lượt từ trên xuống, model nào lỗi thì chuyển sang model kế
MODELS = [
    ("gemini", "gemini-3.5-flash"),
    ("gemini", "gemini-2.5-flash"),
    ("gemini", "gemini-3.5-flash-lite"),
    ("groq", "qwen/qwen3.8-27b"),
    ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free"),
]

# Hạn mức lượt/ngày của từng model dạng "provider/model".
# Để trống thì chỉ hiện số đếm.
LIMITS = {
    "gemini/gemini-3.5-flash": 20,
    "gemini/gemini-2.5-flash": 20,
    "gemini/gemini-3.5-flash-lite": 500,
    "groq/qwen/qwen3.8-27b": None,
    "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free": None,
}

HISTORY_FILE = Path("history") / "current.json"
USAGE_FILE = Path("history") / "usage.json"
