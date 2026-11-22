import json
from datetime import datetime
from pathlib import Path


EXPORT_DIR = Path("exports")


def _clean(name):
    return "".join(c for c in name.strip() if c.isalnum() or c in "-_")


def export_chat(messages, name=""):
    EXPORT_DIR.mkdir(exist_ok=True)
    name = _clean(name) or datetime.now().strftime("chat-%Y%m%d-%H%M")
    json_path = EXPORT_DIR / f"{name}.json"
    md_path = EXPORT_DIR / f"{name}.md"
    json_path.write_text(
        json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = []
    for m in messages:
        who = "Bạn" if m["role"] == "user" else "Bot"
        lines.append(f"**{who}:** {m['text']}\n")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def import_chat(name):
    path = EXPORT_DIR / f"{_clean(name)}.json"
    if not path.is_file():
        return None, f"Không tìm thấy {path}."
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"Không đọc được file: {e}"
    ok = isinstance(data, list) and all(
        isinstance(m, dict)
        and m.get("role") in ("user", "model")
        and isinstance(m.get("text"), str)
        for m in data
    )
    if not ok:
        return None, "File không đúng định dạng lịch sử chat."
    return data, None


def list_exports():
    if not EXPORT_DIR.exists():
        return []
    return sorted(p.stem for p in EXPORT_DIR.glob("*.json"))