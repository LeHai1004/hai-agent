import os
import re
from json import JSONDecodeError
from storage import read_json, update_json, write_json

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
    try:
        data = read_json(path, [])
    except (JSONDecodeError, OSError):
        return []
    return [p for p in data if isinstance(p, str)] if isinstance(data, list) else []


def save_pins(pins, project=None):
    path = _pins_path(project)
    write_json(path, pins)


def add_pin(pins, note, project=None):
    note = note.strip()
    if not note:
        return "Dùng: /pin nội dung cần ghim"
    result = {}

    def add_to_latest(latest):
        current = [pin for pin in latest if isinstance(pin, str)] if isinstance(latest, list) else []
        if len(current) >= MAX_PINS:
            result["reply"] = f"Đã đủ {MAX_PINS} ghim, hãy /unpin số bớt một cái."
            return current
        current.append(note)
        result["reply"] = f"Đã ghim #{len(current)}."
        return current

    pins[:] = update_json(_pins_path(project), [], add_to_latest)
    return result["reply"]


def remove_pin(pins, text, project=None):
    if not text.strip().isdigit():
        return "Dùng: /unpin số (xem số bằng /pins)"
    number = int(text)
    result = {}

    def remove_from_latest(latest):
        current = [pin for pin in latest if isinstance(pin, str)] if isinstance(latest, list) else []
        if not 1 <= number <= len(current):
            result["reply"] = "Dùng: /unpin số (xem số bằng /pins)"
            return current
        removed = current.pop(number - 1)
        result["reply"] = f"Đã bỏ ghim: {removed}"
        return current

    pins[:] = update_json(_pins_path(project), [], remove_from_latest)
    return result["reply"]


def format_pins(pins):
    if not pins:
        return "Chưa có ghim nào."
    return "\n".join(f"  {i}. {p}" for i, p in enumerate(pins, 1))


def pins_as_context(pins):
    # đoạn text đưa vào đầu prompt; rỗng nếu không có ghim
    if not pins:
        return ""
    return "Ghi chú quan trọng người dùng đã ghim, luôn ghi nhớ:\n" + "\n".join(f"- {p}" for p in pins)