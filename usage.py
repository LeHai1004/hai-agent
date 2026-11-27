from datetime import date
from config import LIMITS, USAGE_FILE
from storage import read_json, update_json, write_json


def load_usage():
    today = str(date.today())
    data = read_json(USAGE_FILE, {"date": today, "models": {}})
    if data.get("date") == today:
        return data
    return {"date": today, "models": {}}


def save_usage(usage):
    write_json(USAGE_FILE, usage)


def record_usage(model, meta):
    today = str(date.today())

    def add_usage(usage):
        if usage.get("date") != today:
            usage.clear()
            usage.update({"date": today, "models": {}})
        entry = usage["models"].setdefault(model, {"requests": 0, "tokens": 0})
        entry["requests"] += 1
        if meta and meta.total_token_count:
            entry["tokens"] += meta.total_token_count

    update_json(USAGE_FILE, {"date": today, "models": {}}, add_usage)


def format_usage(usage, provider_model):
    entry = usage["models"].get(provider_model, {"requests": 0, "tokens": 0})
    text = f"{provider_model}: {entry['requests']} lượt, {entry['tokens']} token hôm nay"
    limit = LIMITS.get(provider_model)
    if limit:
        used = entry["requests"]
        percent = min(used * 100 // limit, 100)
        filled = percent // 10
        bar = "█" * filled + "░" * (10 - filled)
        text += f" [{bar}] {used}/{limit} ({percent}%)"
    return text
