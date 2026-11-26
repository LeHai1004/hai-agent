import json
import os
import re

PROJECTS_DIR = os.path.join("history", "projects")
CURRENT_PROJECT_PATH = os.path.join("history", "current_project.txt")
PINS_PATH = os.path.join(PROJECTS_DIR, "default", "pins.json")
PROJECT_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
MAX_PINS = 10  # giới hạn để ghim không phình to, tốn token


def is_valid_project_name(name):
    return (
        isinstance(name, str)
        and name not in ("", ".", "..")
        and PROJECT_NAME_PATTERN.fullmatch(name) is not None
        and "/" not in name
        and "\\" not in name
    )


def _project_name(project=None):
    if project is None:
        try:
            with open(CURRENT_PROJECT_PATH, "r", encoding="utf-8") as f:
                project = f.read().strip()
        except OSError:
            project = "default"
        if not is_valid_project_name(project):
            project = "default"
    elif not is_valid_project_name(project):
        raise ValueError("Tên project không hợp lệ.")
    return project


def _pins_path(project=None):
    return os.path.join(PROJECTS_DIR, _project_name(project), "pins.json")


def load_pins(project=None):
    path = _pins_path(project)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    return [p for p in data if isinstance(p, str)] if isinstance(data, list) else []


def save_pins(pins, project=None):
    path = _pins_path(project)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pins, f, ensure_ascii=False, indent=2)


def add_pin(pins, note, project=None):
    note = note.strip()
    if not note:
        return "Dùng: /pin nội dung cần ghim"
    if len(pins) >= MAX_PINS:
        return f"Đã đủ {MAX_PINS} ghim, hãy /unpin số bớt một cái."
    pins.append(note)
    save_pins(pins, project)
    return f"Đã ghim #{len(pins)}."


def remove_pin(pins, text, project=None):
    if not text.strip().isdigit() or not 1 <= int(text) <= len(pins):
        return "Dùng: /unpin số (xem số bằng /pins)"
    removed = pins.pop(int(text) - 1)
    save_pins(pins, project)
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