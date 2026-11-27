from config import HISTORY_FILE
from storage import read_json, update_json


_DEFAULT_HISTORY = {"messages": [], "summary": "", "summary_upto": 0}


def _history_document(data):
    if isinstance(data, list):
        return {"messages": data, "summary": "", "summary_upto": 0}
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise ValueError("Định dạng lịch sử chat không hợp lệ.")
    summary = data.get("summary", "")
    summary_upto = data.get("summary_upto", 0)
    if not isinstance(summary, str) or type(summary_upto) is not int or summary_upto < 0:
        raise ValueError("Định dạng tóm tắt lịch sử chat không hợp lệ.")
    return {
        "messages": data["messages"],
        "summary": summary,
        "summary_upto": summary_upto,
    }


def load_history_state():
    document = _history_document(read_json(HISTORY_FILE, _DEFAULT_HISTORY))
    return (
        document["messages"],
        document["summary"],
        document["summary_upto"],
    )


def load_history():
    return load_history_state()[0]


def save_history(messages, summary=None, summary_upto=None):
    def replace_messages(data):
        document = _history_document(data)
        document["messages"] = messages
        if summary is not None:
            document["summary"] = summary
        if summary_upto is not None:
            document["summary_upto"] = summary_upto
        return document

    update_json(HISTORY_FILE, _DEFAULT_HISTORY, replace_messages)


def update_history_summary(summary, summary_upto):
    updated = {"value": False}

    def replace_summary(data):
        document = _history_document(data)
        if summary_upto > document["summary_upto"]:
            document["summary"] = summary
            document["summary_upto"] = summary_upto
            updated["value"] = True
        return document

    update_json(HISTORY_FILE, _DEFAULT_HISTORY, replace_summary)
    return updated["value"]
