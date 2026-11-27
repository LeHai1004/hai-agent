from config import HISTORY_FILE
from storage import read_json, write_json


def load_history():
    return read_json(HISTORY_FILE, [])


def save_history(messages):
    write_json(HISTORY_FILE, messages)
