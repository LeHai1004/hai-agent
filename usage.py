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
