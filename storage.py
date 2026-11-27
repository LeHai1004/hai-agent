import copy
import json
import os
import tempfile
import threading


_PATH_LOCKS = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_for(path):
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _read_json_unlocked(path, default):
    if not os.path.exists(path):
        return copy.deepcopy(default)
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def read_json(path, default):
    path = os.fspath(path)
    with _lock_for(path):
        return _read_json_unlocked(path, default)


def _write_json_unlocked(path, data):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{os.path.basename(path)}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = file.name
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def write_json(path, data):
    path = os.fspath(path)
    with _lock_for(path):
        _write_json_unlocked(path, data)


def update_json(path, default, fn):
    path = os.fspath(path)
    with _lock_for(path):
        data = _read_json_unlocked(path, default)
        replacement = fn(data)
        if replacement is not None:
            data = replacement
        _write_json_unlocked(path, data)
        return data
