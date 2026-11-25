import json
import os

PINS_PATH = os.path.join("history", "pins.json")
MAX_PINS = 10  # giới hạn để ghim không phình to, tốn token


def load_pins():
    if not os.path.exists(PINS_PATH):
        return []
    try:
        with open(PINS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    return [p for p in data if isinstance(p, str)] if isinstance(data, list) else []


def save_pins(pins):
    os.makedirs("history", exist_ok=True)
    with open(PINS_PATH, "w", encoding="utf-8") as f:
        json.dump(pins, f, ensure_ascii=False, indent=2)


def add_pin(pins, note):
    note = note.strip()
    if not note:
        return "Dùng: /pin nội dung cần ghim"
    if len(pins) >= MAX_PINS:
        return f"Đã đủ {MAX_PINS} ghim, hãy /unpin số bớt một cái."
    pins.append(note)
    save_pins(pins)
    return f"Đã ghim #{len(pins)}."


def remove_pin(pins, text):
    if not text.strip().isdigit() or not 1 <= int(text) <= len(pins):
        return "Dùng: /unpin số (xem số bằng /pins)"
    removed = pins.pop(int(text) - 1)
    save_pins(pins)
    return f"Đã bỏ ghim: {removed}"


def format_pins(pins):
    if not pins:
        return "Chưa có ghim nào."
    return "\n".join(f"  {i}. {p}" for i, p in enumerate(pins, 1))


def pins_as_context(pins):
    # đoạn text đưa vào đầu prompt; rỗng nếu không có ghim
    if not pins:
        return ""
    return "Ghi chú quan trọng người dùng đã ghim, luôn ghi nhớ:\n" + "\n".join(f"- {p}" for p in pins)