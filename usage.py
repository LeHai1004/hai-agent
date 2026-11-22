import json
from datetime import date
from config import LIMITS, USAGE_FILE


def load_usage():
    today = str(date.today())
    if USAGE_FILE.exists():
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        if data.get("date") == today:
            return data
    return {"date": today, "models": {}}


def save_usage(usage):
    USAGE_FILE.parent.mkdir(exist_ok=True)
    USAGE_FILE.write_text(
        json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def record_usage(usage, model, meta):
    entry = usage["models"].setdefault(model, {"requests": 0, "tokens": 0})
    entry["requests"] += 1
    if meta and meta.total_token_count:
        entry["tokens"] += meta.total_token_count
    save_usage(usage)


def format_usage(usage, model):
    entry = usage["models"].get(model, {"requests": 0, "tokens": 0})
    text = f"{model}: {entry['requests']} lượt, {entry['tokens']} token hôm nay"
    limit = LIMITS.get(model)
    if limit:
        percent = entry["requests"] * 100 // limit
        text += f" ({entry['requests']}/{limit} lượt, {percent}%)"
    return text
