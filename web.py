import html
import json
import os
import re
import shutil
import threading
import time
import uuid
from collections import deque
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import tools
from pins import (
    add_pin,
    format_pins,
    is_valid_project_name,
    load_pins,
    remove_pin,
    save_pins,
)
from project_workspace import get_workspace_dir
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from transfer import EXPORT_DIR, export_chat, import_chat, list_exports
from google import genai
from google.genai import types
from openai import OpenAI
from werkzeug.utils import secure_filename

from history import load_history, save_history
from llm import ask
from memory import (
    MAX_MEMORIES,
    add_memory,
    add_memories,
    build_deletion_prompt,
    build_extraction_prompt,
    delete_memory,
    delete_memory_by_number,
    edit_memory,
    extract_memories as validate_extracted_memories,
    has_memory_hint,
    list_memories,
    memory_intent,
    memory_log,
    parse_delete_ids,
)
from filechat import ask_file
from img import ask_image
from pdfread import parse_read_command, read_pdf_page
from search import build_prompt, web_search
from translate import translate
from usage import format_usage, load_usage, record_usage
from visual import parse_scenes, play, visualize

load_dotenv()
app = Flask(__name__)
MAX_UPLOAD_SIZE = 30 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".txt", ".md", ".py",
    ".java", ".kt", ".cs", ".json", ".xml", ".html", ".css", ".js", ".sql",
}
IMAGE_UPLOAD_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
DOWNLOADABLE_WORKSPACE_EXTENSIONS = {".docx", ".md", ".txt"}
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE + 64 * 1024
lock = threading.Lock()
chat_jobs_lock = threading.Lock()
chat_jobs = {}
active_job_id = None
memory_extraction_jobs = set()
memory_extraction_jobs_lock = threading.Lock()
memory_completed_messages = {}
memory_recent_events = deque(maxlen=200)
memory_recent_events_lock = threading.Lock()
confirm_context = threading.local()
PROJECTS_DIR = Path("history", "projects")
CURRENT_PROJECT_PATH = Path("history", "current_project.txt")
LEGACY_CHAT_DIR = Path("history", "chats")
CHAT_ID_PATTERN = re.compile(r"\d{8}_\d{4}(?:\d{2}(?:_\d{6})?)?")


def _load_current_project():
    try:
        name = CURRENT_PROJECT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "default"
    project_dir = PROJECTS_DIR / name if is_valid_project_name(name) else None
    if project_dir is None or project_dir.is_symlink() or not project_dir.is_dir():
        return "default"
    return name


current_project = _load_current_project()


def _project_dir(project=None):
    name = current_project if project is None else project
    if not is_valid_project_name(name):
        raise ValueError("Tên project không hợp lệ.")
    return PROJECTS_DIR / name


def _chat_dir(project=None):
    return _project_dir(project) / "chats"


def _ensure_project_layout(project):
    project_dir = _project_dir(project)
    if project_dir.is_symlink():
        raise ValueError("Thư mục project không hợp lệ.")
    project_dir.mkdir(parents=True, exist_ok=True)
    chats_dir = _chat_dir(project)
    if chats_dir.is_symlink():
        raise ValueError("Thư mục chat của project không hợp lệ.")
    chats_dir.mkdir(exist_ok=True)
    pins_path = project_dir / "pins.json"
    if pins_path.is_symlink():
        raise ValueError("Tệp ghim của project không hợp lệ.")
    if not pins_path.exists():
        save_pins([], project)


def _migrate_legacy_data():
    default_dir = _project_dir("default")
    default_dir.mkdir(parents=True, exist_ok=True)
    default_chats = _chat_dir("default")
    default_chats.mkdir(exist_ok=True)

    legacy_pins = Path("history", "pins.json")
    default_pins = default_dir / "pins.json"
    if not default_pins.exists():
        if legacy_pins.is_file() and not legacy_pins.is_symlink():
            shutil.copy2(legacy_pins, default_pins)
        else:
            save_pins([], "default")

    if LEGACY_CHAT_DIR.is_dir() and not LEGACY_CHAT_DIR.is_symlink():
        for path in LEGACY_CHAT_DIR.glob("*.json"):
            if (
                CHAT_ID_PATTERN.fullmatch(path.stem)
                and path.is_file()
                and not path.is_symlink()
            ):
                destination = default_chats / path.name
                if not destination.exists():
                    shutil.copy2(path, destination)


def _save_current_project():
    CURRENT_PROJECT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = CURRENT_PROJECT_PATH.with_suffix(".txt.tmp")
    temporary_path.write_text(current_project + "\n", encoding="utf-8")
    temporary_path.replace(CURRENT_PROJECT_PATH)


def _list_projects():
    projects = [
        path.name
        for path in PROJECTS_DIR.iterdir()
        if is_valid_project_name(path.name)
        and path.is_dir()
        and not path.is_symlink()
    ]
    return sorted(projects, key=lambda name: (name != "default", name.lower()))


def _switch_project(name):
    global current_project, current_chat_id, current_extracted_upto, messages
    if name == current_project:
        return
    _save_current_chat()
    _schedule_memory_extraction(
        current_project, current_chat_id, messages, current_extracted_upto)
    messages = []
    current_chat_id = None
    current_extracted_upto = 0
    save_history(messages)
    current_project = name
    _save_current_project()


def looks_personal(text):
    text = text.strip()
    if not 12 <= len(text) <= 200 or text.startswith("/"):
        return False
    lowered = text.lower()
    if lowered.endswith("?") or any(
        phrase in lowered for phrase in ("là gì", "như thế nào", "tại sao")
    ):
        return False
    patterns = (
        r"\b(tôi|mình|em)\s+(đang|vừa|sắp|đã)?\s*(học|làm|dùng|thích|muốn|cần|tên|ở|là)\b",
        r"(tên (tôi|mình)|dự án của (tôi|mình)|mục tiêu của (tôi|mình))",
        r"(từ giờ|luôn luôn|lần sau|nhớ giúp)",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def _workspace_file_mtimes():
    workspace = get_workspace_dir()
    if not workspace.is_dir():
        return {}
    return {
        path.name: path.stat().st_mtime_ns
        for path in workspace.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.lower() in DOWNLOADABLE_WORKSPACE_EXTENSIONS
    }


def _chat_path(chat_id):
    if not isinstance(chat_id, str) or not CHAT_ID_PATTERN.fullmatch(chat_id):
        return None
    return _chat_dir() / f"{chat_id}.json"


def _read_chat(path):
    chat = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(chat, dict)
        or not isinstance(chat.get("title"), str)
        or not isinstance(chat.get("updated"), str)
        or not isinstance(chat.get("messages"), list)
        or type(chat.get("extracted_upto", 0)) is not int
        or chat.get("extracted_upto", 0) < 0
    ):
        raise ValueError(f"Định dạng lịch sử chat không hợp lệ: {path.name}")
    return chat


def _list_chats():
    chat_dir = _chat_dir()
    if not chat_dir.is_dir():
        return []
    chats = []
    for path in chat_dir.glob("*.json"):
        if (
            _chat_path(path.stem) is None
            or path.is_symlink()
            or not path.is_file()
        ):
            continue
        chat = _read_chat(path)
        chats.append({
            "id": path.stem,
            "title": chat["title"],
            "updated": chat["updated"],
            "current": path.stem == current_chat_id,
        })
    return sorted(chats, key=lambda chat: (chat["updated"], chat["id"]), reverse=True)


def _save_current_chat():
    global current_chat_id
    first_user_message = next(
        (
            item.get("text")
            for item in messages
            if isinstance(item, dict)
            and item.get("role") == "user"
            and isinstance(item.get("text"), str)
        ),
        None,
    )
    if first_user_message is None:
        return
    if current_chat_id is None:
        current_chat_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    chat_dir = _chat_dir()
    chat_dir.mkdir(parents=True, exist_ok=True)
    path = _chat_path(current_chat_id)
    if path is None:
        raise ValueError("Mã cuộc chat không hợp lệ.")
    chat = {
        "title": first_user_message[:40],
        "updated": datetime.now().isoformat(timespec="microseconds"),
        "messages": messages,
        "extracted_upto": current_extracted_upto,
    }
    temporary_path = path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(chat, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary_path.replace(path)


def _find_saved_chat(messages):
    chat_dir = _chat_dir()
    if not chat_dir.is_dir():
        return None
    chats = []
    for path in chat_dir.glob("*.json"):
        if (
            _chat_path(path.stem) is None
            or path.is_symlink()
            or not path.is_file()
        ):
            continue
        chat = _read_chat(path)
        if chat["messages"] == messages:
            chats.append((path.stem, chat))
    if not chats:
        return None
    return max(chats, key=lambda item: (item[1]["updated"], item[0]))


def _record_memory_event(action, text):
    event = {
        "id": uuid.uuid4().hex,
        "action": action,
        "text": text,
        "timestamp": int(time.time() * 1000),
    }
    with memory_recent_events_lock:
        memory_recent_events.append(event)


def _mark_memory_upto_locked(project, chat_id, message_index):
    global current_extracted_upto
    if not isinstance(chat_id, str) or not CHAT_ID_PATTERN.fullmatch(chat_id):
        memory_log("[memory] lỗi: mã chat không hợp lệ khi cập nhật extracted_upto")
        return
    path = _chat_dir(project) / f"{chat_id}.json"
    if path.is_symlink() or not path.is_file():
        memory_log(f"[memory] lỗi: không tìm thấy file chat {chat_id}")
        return
    chat = _read_chat(path)
    persisted_upto = chat.get("extracted_upto", 0)
    completed_key = (project, chat_id)
    with memory_extraction_jobs_lock:
        completed = memory_completed_messages.setdefault(completed_key, set())
        if message_index > persisted_upto:
            completed.add(message_index)
        next_upto = persisted_upto
        while next_upto + 1 in completed:
            next_upto += 1
            completed.remove(next_upto)
        if not completed:
            memory_completed_messages.pop(completed_key, None)
    if next_upto == persisted_upto:
        return
    chat["extracted_upto"] = next_upto
    temporary_path = path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(chat, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary_path.replace(path)
    if current_project == project and current_chat_id == chat_id:
        current_extracted_upto = next_upto


def _mark_memory_upto(project, chat_id, message_index):
    with lock:
        _mark_memory_upto_locked(project, chat_id, message_index)


def extract_memories(project, chat_id, user_message, message_index, job_key):
    try:
        intent = memory_intent(user_message)
        if not has_memory_hint(user_message):
            memory_log("[memory] bỏ qua: không có dấu hiệu")
            _mark_memory_upto(project, chat_id, message_index)
            return
        existing_memories = list_memories()
        if intent == "forget":
            prompt = build_deletion_prompt(user_message, existing_memories)
            operation = "xóa"
        else:
            prompt = build_extraction_prompt(
                [user_message],
                existing_memories,
                explicit=intent == "remember",
            )
            operation = "nhớ" if intent == "remember" else "trích xuất"
        memory_log(
            f"[memory] gọi model gemini-3.5-flash-lite để {operation}: "
            f"{user_message[:240]!r}"
        )
        reply, model_key, meta = ask(
            gemini_client,
            openai_clients,
            [],
            prompt,
            use_history=False,
            route_text="trích xuất bộ nhớ",
            model_override="gemini/gemini-3.5-flash-lite",
            include_persistent_context=False,
            allow_tools=False,
        )
        memory_log(f"[memory] model trả: {reply[:600]!r}")
        with lock:
            record_usage(usage, model_key, meta)
        if intent == "forget":
            delete_ids = parse_delete_ids(reply, existing_memories)
            if delete_ids is None:
                return
            existing_by_id = {item["id"]: item for item in existing_memories}
            deleted = []
            for memory_id in delete_ids:
                item = existing_by_id[memory_id]
                if delete_memory(memory_id):
                    deleted.append(item)
                    _record_memory_event("forgot", item["text"])
                else:
                    memory_log(f"[memory] bỏ xóa: mục đã biến mất {memory_id}")
            if deleted:
                for item in deleted:
                    memory_log(f"[memory] đã quên: {item['text']}")
            else:
                memory_log("[memory] không có mục phù hợp để quên")
        else:
            facts = validate_extracted_memories(
                reply, [user_message], existing_memories)
            if facts is None:
                return
            added, full = add_memories(facts)
            for item in added:
                _record_memory_event("remembered", item["text"])
            memory_log(f"[memory] lưu {len(added)} mục")
            if full:
                memory_log("[memory] bộ nhớ đầy; từ chối các mục mới còn lại")
            if not added and not full:
                memory_log("[memory] bỏ qua: không có fact mới hợp lệ")
        _mark_memory_upto(project, chat_id, message_index)
    except Exception as error:
        safe_error = str(error).encode("ascii", "backslashreplace").decode("ascii")
        memory_log(f"[memory] lỗi: {safe_error}")
    finally:
        with memory_extraction_jobs_lock:
            memory_extraction_jobs.discard(job_key)


def _schedule_memory_message(project, chat_id, user_message, message_index):
    if chat_id is None:
        memory_log("[memory] bỏ qua: chưa có chat id")
        return
    if not has_memory_hint(user_message):
        memory_log("[memory] bỏ qua: không có dấu hiệu")
        _mark_memory_upto_locked(project, chat_id, message_index)
        return
    job_key = (project, chat_id, message_index)
    with memory_extraction_jobs_lock:
        completed_key = (project, chat_id)
        if (
            project == current_project
            and chat_id == current_chat_id
            and message_index <= current_extracted_upto
        ) or message_index in memory_completed_messages.get(completed_key, set()):
            memory_log(f"[memory] bỏ qua: tin nhắn {message_index} đã xử lý")
            return
        if job_key in memory_extraction_jobs:
            memory_log(f"[memory] bỏ qua: tin nhắn {message_index} đang xử lý")
            return
        memory_extraction_jobs.add(job_key)
    try:
        threading.Thread(
            target=extract_memories,
            args=(project, chat_id, user_message, message_index, job_key),
            daemon=True,
        ).start()
        memory_log(f"[memory] đã lên lịch tin nhắn {message_index}")
    except Exception as error:
        with memory_extraction_jobs_lock:
            memory_extraction_jobs.discard(job_key)
        safe_error = str(error).encode("ascii", "backslashreplace").decode("ascii")
        memory_log(f"[memory] lỗi tạo thread: {safe_error}")


def _schedule_memory_extraction(project, chat_id, chat_messages, extracted_upto):
    if chat_id is None:
        memory_log("[memory] bỏ qua chat rời: chưa có chat id")
        return
    user_messages = [
        item["text"]
        for item in chat_messages
        if isinstance(item, dict)
        and item.get("role") == "user"
        and isinstance(item.get("text"), str)
    ]
    if type(extracted_upto) is not int or extracted_upto < 0:
        memory_log("[memory] lỗi: extracted_upto không hợp lệ")
        return
    upto = min(extracted_upto, len(user_messages))
    if upto >= len(user_messages):
        memory_log("[memory] bỏ qua chat rời: không có tin nhắn mới")
        return
    for index, user_message in enumerate(user_messages[upto:], start=upto + 1):
        _schedule_memory_message(project, chat_id, user_message, index)


def make_clients():
    gemini_client = None
    if os.environ.get("GEMINI_API_KEY"):
        gemini_client = genai.Client(
            api_key=os.environ["GEMINI_API_KEY"],
            http_options=types.HttpOptions(api_version="v1beta"),
        )
    openai_clients = {}
    if os.environ.get("GROQ_API_KEY"):
        openai_clients["groq"] = OpenAI(
            api_key=os.environ["GROQ_API_KEY"], base_url="https://api.groq.com/openai/v1")
    if os.environ.get("OPENROUTER_API_KEY"):
        openai_clients["openrouter"] = OpenAI(
            api_key=os.environ["OPENROUTER_API_KEY"], base_url="https://openrouter.ai/api/v1")
    return gemini_client, openai_clients


_migrate_legacy_data()
_ensure_project_layout(current_project)
_save_current_project()

gemini_client, openai_clients = make_clients()
current_chat_id = None
current_extracted_upto = 0
messages = load_history()
if messages:
    saved_chat = _find_saved_chat(messages)
    if saved_chat is not None:
        current_chat_id = saved_chat[0]
        current_extracted_upto = saved_chat[1].get("extracted_upto", 0)
    else:
        # Migrate the existing single-history chat without creating empty chats.
        _save_current_chat()
usage = load_usage()


def _web_confirm_handler(tool_name, reason, suggestion):
    job_id = getattr(confirm_context, "job_id", None)
    if job_id is None:
        return False
    with chat_jobs_lock:
        job = chat_jobs.get(job_id)
        if job is None or job["status"] != "running":
            return False
        job["confirmation_id"] += 1
        job["confirmation"] = {
            "id": job["confirmation_id"],
            "tool": tool_name,
            "reason": reason,
            "suggestion": suggestion,
        }
        job["deadline"] = time.monotonic() + 60
        job["decision"] = None
        job["status"] = "confirmation"
        job["decision_event"].clear()
        deadline = job["deadline"]

    signaled = job["decision_event"].wait(max(0, deadline - time.monotonic()))
    with chat_jobs_lock:
        timed_out = not signaled or time.monotonic() >= deadline
        approved = (
            not timed_out
            and job["decision"] is True
        )
        job["status"] = "running"
        job["confirmation"] = None
        job["decision"] = None
    if timed_out:
        raise tools.ConfirmationTimeoutError(
            "Hết thời gian xác nhận sau 60 giây; thao tác đã được hủy."
        )
    return approved


tools.CONFIRM_HANDLER = _web_confirm_handler


PAGE = """<!DOCTYPE html>
<html lang="vi"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hai Agent</title>
<style>
*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;flex-direction:column;background:#1b1c1f;color:#e8e8ea;font-family:system-ui,sans-serif}
header{display:flex;justify-content:space-between;align-items:center;padding:12px 20px;border-bottom:1px solid #2e3036}
header b{font-size:1.05rem}
.header-actions{display:flex;gap:8px}
button{border:0;border-radius:8px;padding:9px 16px;background:#3b82f6;color:#fff;cursor:pointer;font-size:.95rem}
button:disabled{background:#4b5060;cursor:not-allowed}
.ghost{background:#2e3036}
#log{flex:1;overflow-y:auto;padding:20px;max-width:860px;width:100%;margin:0 auto}
#layout{display:flex;flex:1;min-height:0;width:100%}
#chat-sidebar{display:flex;flex:0 0 220px;flex-direction:column;min-height:0;background:#202125;border-right:1px solid #2e3036}
body.sidebar-collapsed #chat-sidebar{display:none}
.sidebar-head{padding:12px;border-bottom:1px solid #2e3036}
#chat-list{flex:1;overflow-y:auto;padding:8px}
.chat-item{display:flex;gap:4px;margin-bottom:6px}
.chat-item.active .chat-open{background:#2a3a55}
.chat-open{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:left;background:#2e3036;padding:8px}
.chat-delete{flex:0 0 30px;padding:4px;background:#2e3036;color:#aaa}
.chat-delete:hover{background:#7f1d1d;color:#fff}
.project-controls{display:flex;gap:6px;margin-bottom:10px}
#project-select{flex:1;min-width:0;padding:6px;border:1px solid #3a3d45;border-radius:6px;background:#24262b;color:#e8e8ea}
#create-project{padding:6px 8px;font-size:.78rem;white-space:nowrap}
#memory-open{width:100%;margin-top:10px}
#chat{display:flex;flex:1 1 auto;flex-direction:column;min-width:0}
#splitter{display:none;flex:0 0 6px;cursor:col-resize;background:#2e3036;z-index:2;touch-action:none;transition:background .12s,box-shadow .12s}
body.panel-open #splitter{display:block}
#splitter:hover,body.panel-resizing #splitter{background:#3b82f6;box-shadow:0 0 8px #3b82f6}
#panel{display:none;flex:0 0 55%;flex-direction:column;min-width:300px;max-width:70vw;min-height:0;border-left:1px solid #2e3036;background:#202125}
body.panel-open #panel{display:flex}
body.panel-resizing,body.panel-resizing *{cursor:col-resize!important;user-select:none!important}
body.panel-resizing #panel-frame{pointer-events:none}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 12px;border-bottom:1px solid #2e3036}
.panel-head b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#close-panel{font-size:1.1rem;padding:7px 12px}
#panel-frame{flex:1;width:100%;min-height:0;border:0;background:#fff}
.msg{margin-bottom:16px;line-height:1.6;word-wrap:break-word}
.user{background:#2a3a55;padding:10px 14px;border-radius:12px;margin-left:auto;width:fit-content;max-width:80%;white-space:pre-wrap}
.user .thumbnail{display:block;max-width:180px;max-height:120px;object-fit:contain;margin-top:8px;border-radius:6px}
.bot{padding:4px 2px}
.meta{color:#8b8f99;font-size:.78rem;margin-top:4px}
.confirmation{padding:12px;background:#24262b;border:1px solid #4b5060;border-radius:10px;white-space:pre-wrap}
.confirmation-actions{display:flex;gap:8px;margin-top:10px}
.confirmation-status{color:#aeb4c0;font-size:.85rem;margin-top:8px}
.pin-suggestion{display:flex;align-items:center;gap:8px;margin-top:8px}
.pin-suggestion button{padding:4px 8px;font-size:.78rem;border-radius:6px}
.pin-suggestion-status{color:#aeb4c0;font-size:.8rem}
.memory-result{color:#8b8f99;font-size:.8rem;margin-top:6px}
.export-list{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.export-list button{padding:6px 10px;font-size:.85rem}
.export-list-status{color:#aeb4c0;font-size:.8rem;margin-top:8px}
pre{background:#111214;padding:12px;border-radius:8px;overflow-x:auto}
h3,h4,h5{font-weight:700;line-height:1.3;margin:8px 0 4px}
h3{font-size:1.15rem}
h4{font-size:1.08rem}
h5{font-size:1rem}
hr{border:0;border-top:1px solid #555;opacity:.6;margin:8px 0}
code{background:#111214;padding:1px 5px;border-radius:4px;font-family:Consolas,monospace}
pre code{padding:0}
a{color:#7cb2ff}
footer{border-top:1px solid #2e3036;padding:14px 20px}
.row{display:flex;gap:10px;max-width:860px;margin:0 auto}
textarea{flex:1;resize:none;height:56px;padding:12px;border-radius:10px;border:1px solid #3a3d45;background:#24262b;color:#e8e8ea;font-size:1rem;font-family:inherit}
.hint{max-width:860px;margin:6px auto 0;color:#8b8f99;font-size:.78rem}
</style></head><body>
<header><b>Hai Agent</b><div class="header-actions">
<button class="ghost" id="toggle-sidebar" aria-label="Thu gọn danh sách chat">☰</button>
<button class="ghost" id="new">Chat mới</button></div></header>
<main id="layout">
<aside id="chat-sidebar"><div class="sidebar-head">
<div class="project-controls">
<select id="project-select" aria-label="Project"></select>
<button type="button" class="ghost" id="create-project">+ Project mới</button>
</div>
<b>Cuộc chat</b>
<button type="button" class="ghost" id="memory-open">🧠 Bộ nhớ</button>
</div><div id="chat-list"></div></aside>
<section id="chat">
<div id="log"></div>
<footer>
<div class="row"><textarea id="box" placeholder="Nhắn tin... (Enter gửi, Shift+Enter xuống dòng, /web câu hỏi để tìm web)"></textarea>
<button type="button" class="ghost" id="attach" title="Chọn file" aria-label="Chọn file">📎</button>
<button id="send">Gửi</button></div>
<input type="file" id="pdf-file" accept=".pdf,.png,.jpg,.jpeg,.webp,.txt,.md,.py,.java,.kt,.cs,.json,.xml,.html,.css,.js,.sql" hidden>
<div class="hint" id="usage"></div>
</footer>
</section>
<div id="splitter" role="separator" aria-orientation="vertical" aria-label="Thay đổi độ rộng khung bên"></div>
<aside id="panel">
<div class="panel-head"><b id="panel-title"></b><button class="ghost" id="close-panel" aria-label="Đóng panel">✕</button></div>
<iframe id="panel-frame" title="Nội dung" allow="autoplay; speech-synthesis"></iframe>
</aside>
</main>
<script>
const log = document.getElementById("log");
const box = document.getElementById("box");
const sendBtn = document.getElementById("send");
const panel = document.getElementById("panel");
const panelFrame = document.getElementById("panel-frame");
const panelTitle = document.getElementById("panel-title");
const splitter = document.getElementById("splitter");
const chatList = document.getElementById("chat-list");
const projectSelect = document.getElementById("project-select");
const attachBtn = document.getElementById("attach");
const pdfFileInput = document.getElementById("pdf-file");
let pendingImagePreview = null;
const panelWidthKey = "hai-agent-panel-width";
function setPanelWidth(width){
  const maxWidth = Math.max(300, window.innerWidth * 0.7);
  const boundedWidth = Math.max(300, Math.min(maxWidth, width));
  panel.style.flexBasis = boundedWidth + "px";
}
const savedPanelWidth = Number(localStorage.getItem(panelWidthKey));
if (Number.isFinite(savedPanelWidth) && savedPanelWidth > 0) {
  setPanelWidth(savedPanelWidth);
}
let resizingPanel = false;
splitter.addEventListener("mousedown", event => {
  if (event.button !== 0) return;
  event.preventDefault();
  resizingPanel = true;
  document.body.classList.add("panel-resizing");
});
document.addEventListener("mousemove", event => {
  if (resizingPanel) setPanelWidth(window.innerWidth - event.clientX);
});
document.addEventListener("mouseup", () => {
  if (!resizingPanel) return;
  resizingPanel = false;
  document.body.classList.remove("panel-resizing");
  localStorage.setItem(panelWidthKey, parseFloat(panel.style.flexBasis));
});
splitter.addEventListener("dblclick", () => {
  panel.style.flexBasis = "55%";
  localStorage.removeItem(panelWidthKey);
});
window.addEventListener("resize", () => {
  const currentBasis = panel.style.flexBasis;
  if (currentBasis.endsWith("px")) setPanelWidth(parseFloat(currentBasis));
});
function openPanel(url, title){
  panelTitle.textContent = title;
  panelFrame.src = /[?&]t=/.test(url) ? url : url + (url.includes("?") ? "&" : "?") + "t=" + Date.now();
  document.body.classList.add("panel-open");
}
document.getElementById("close-panel").addEventListener("click", () => {
  document.body.classList.remove("panel-open");
});
const panelTitles = {"/bilingual":"Dịch song ngữ","/read":"Đọc tài liệu","/vis":"Trực quan hóa","/vis-play":"Giải thích động","/vis-view":"Sơ đồ giải thích","/diagram":"Sơ đồ","/memory":"Bộ nhớ"};
log.addEventListener("click", event => {
  const link = event.target.closest("a");
  if (!link) return;
  const href = link.getAttribute("href");
  if (!href) return;
  const pdfMatch = href.match(/^\\/pdfview\\/(\\d+)$/);
  const title = panelTitles[href] || (pdfMatch ? `PDF trang ${pdfMatch[1]}` : "");
  if (!title) return;
  event.preventDefault();
  openPanel(href, title);
});
function render(t){
  const esc = s => s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  const inline = line => line
    .replace(/`([^`]+)`/g,"<code>$1</code>")
    .replace(/\\*\\*([^*]+)\\*\\*/g,"<b>$1</b>")
    .replace(/(^|[\\s:])\\/bilingual\\b/g,'$1<a href="/bilingual">/bilingual</a>')
    .replace(/(^|[\\s:])\\/diagram\\b/g,'$1<a href="/diagram">/diagram</a>')
    .replace(/(^|[\\s:])\\/pdfview\\/(\\d+)\\b/g,'$1<a href="/pdfview/$2">/pdfview/$2</a>')
    .replace(/(^|[\\s:])\\/(vis-play|vis-view)\\b/g,'$1<a href="/$2">/$2</a>')
    .replace(/(^|[\\s:])\\/download\\/([^\\s<"']+)/g,'$1<a href="/download/$2">/download/$2</a>')
    .replace(/(^|[\\s:])\\/files\\/([^\\s<"']+)/g,'$1<a href="/files/$2">/files/$2</a>')
    .replace(/(https?:\\/\\/[^\\s<"']+)/g,'<a href="$1" target="_blank" rel="noopener">$1</a>');
  return esc(t).split("```").map((p,i) => i % 2
    ? "<pre><code>" + p.replace(/^[a-zA-Z0-9]*\\n/,"") + "</code></pre>"
    : p.split("\\n").map(line => {
        const heading = line.match(/^(#{1,3}) (.+)$/);
        if (heading) {
          const level = 2 + heading[1].length;
          return `<h${level}>${inline(heading[2])}</h${level}>`;
        }
        if (line === "---") return "<hr>";
        return inline(line);
      }).join("<br>")).join("");
}
function addMsg(role, text, meta, imageUrl){
  const d = document.createElement("div");
  d.className = "msg " + (role === "user" ? "user" : "bot");
  if (role === "user") {
    d.textContent = text;
    if (imageUrl) {
      const img = document.createElement("img");
      img.className = "thumbnail";
      img.alt = "Ảnh đã tải lên";
      img.src = imageUrl;
      d.appendChild(img);
    }
  } else d.innerHTML = render(text);
  if (meta){ const m = document.createElement("div"); m.className = "meta"; m.textContent = meta; d.appendChild(m); }
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
  return d;
}
async function pollMemoryRecent(since, botMessage){
  const seen = new Set();
  for (let attempt = 0; attempt < 4; attempt++) {
    if (attempt > 0) await new Promise(resolve => setTimeout(resolve, 2000));
    try {
      const response = await fetch(`/memory/recent?since=${encodeURIComponent(since)}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Không kiểm tra được bộ nhớ");
      for (const event of data.events || []) {
        if (seen.has(event.id)) continue;
        seen.add(event.id);
        const note = document.createElement("div");
        note.className = "memory-result";
        const label = event.action === "remembered" ? "🧠 Đã nhớ: " : "🧠 Đã quên: ";
        note.textContent = label + event.text;
        botMessage.appendChild(note);
      }
      log.scrollTop = log.scrollHeight;
    } catch (error) {
      console.error("Không lấy được kết quả bộ nhớ:", error);
    }
  }
}
function addPinSuggestion(message, suggestion){
  const container = document.createElement("div");
  container.className = "pin-suggestion";
  const actions = document.createElement("div");
  const pinButton = document.createElement("button");
  pinButton.type = "button";
  pinButton.textContent = "📌 Ghim câu này";
  const dismissButton = document.createElement("button");
  dismissButton.type = "button";
  dismissButton.className = "ghost";
  dismissButton.textContent = "✕";
  dismissButton.setAttribute("aria-label", "Bỏ qua gợi ý ghim");
  const status = document.createElement("span");
  status.className = "pin-suggestion-status";
  pinButton.addEventListener("click", async () => {
    pinButton.disabled = true;
    status.textContent = "Đang ghim...";
    try {
      const response = await fetch("/api/chat", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({text:"/pin " + suggestion})
      });
      let data = await response.json();
      if (!response.ok) throw new Error(data.error || "Không thể ghim");
      if (data.pending) data = await waitForChat(data.request_id);
      if (typeof data.reply === "string" && data.reply.startsWith("Đã ghim #")) {
        status.textContent = "Đã ghim";
        actions.hidden = true;
      } else {
        status.textContent = data.reply || "Không thể ghim";
        pinButton.disabled = false;
      }
    } catch (e) {
      status.textContent = "Lỗi: " + e.message;
      pinButton.disabled = false;
    }
  });
  dismissButton.addEventListener("click", () => container.remove());
  actions.className = "pin-suggestion-actions";
  actions.append(pinButton, dismissButton);
  container.append(actions, status);
  message.appendChild(container);
}
function addExportButtons(message, exports){
  const container = document.createElement("div");
  container.className = "export-list";
  const status = document.createElement("div");
  status.className = "export-list-status";
  const buttons = exports.map(name => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = name + ".json";
    button.addEventListener("click", async () => {
      buttons.forEach(item => { item.disabled = true; });
      status.textContent = "Đang nạp...";
      try {
        const response = await fetch("/api/chat", {
          method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({text:"/import " + name})
        });
        let data = await response.json();
        if (!response.ok) throw new Error(data.error || "Không thể nạp lịch sử");
        if (data.pending) data = await waitForChat(data.request_id);
        if (data.new && Array.isArray(data.history)) {
          log.innerHTML = "";
          data.history.forEach(item => addMsg(
            item.role === "user" ? "user" : "bot", item.text
          ));
          addMsg("bot", data.reply || `Đã nạp ${data.history.length} tin nhắn`);
          return;
        }
        status.textContent = data.reply || "Không thể nạp lịch sử";
        buttons.forEach(item => { item.disabled = false; });
      } catch (e) {
        status.textContent = "Lỗi: " + e.message;
        buttons.forEach(item => { item.disabled = false; });
      }
    });
    container.appendChild(button);
    return button;
  });
  container.appendChild(status);
  message.appendChild(container);
}
function addConfirmation(requestId, confirmation){
  const card = document.createElement("div");
  card.className = "msg bot confirmation";
  const prompt = document.createElement("div");
  prompt.textContent = confirmation.suggestion
    ? `⚠ Tool '${confirmation.tool}' ${confirmation.reason}.\nGợi ý: ${confirmation.suggestion}.\n\nVẫn chạy?`
    : `⚠ Tool '${confirmation.tool}': ${confirmation.reason}\n\nVẫn chạy?`;
  const actions = document.createElement("div");
  actions.className = "confirmation-actions";
  const approve = document.createElement("button");
  approve.textContent = "Đồng ý";
  const cancel = document.createElement("button");
  cancel.className = "ghost";
  cancel.textContent = "Hủy";
  const status = document.createElement("div");
  status.className = "confirmation-status";
  async function decide(approved){
    approve.disabled = true;
    cancel.disabled = true;
    status.textContent = "Đang gửi xác nhận...";
    try {
      const response = await fetch("/confirm", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          request_id:requestId,
          confirmation_id:confirmation.id,
          approved
        })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "Yêu cầu xác nhận đã hết hạn");
      status.textContent = approved ? "Đã đồng ý. Đang tiếp tục..." : "Đã hủy. Đang xử lý...";
    } catch (e) {
      status.textContent = "Không gửi được xác nhận: " + e.message;
      approve.disabled = false;
      cancel.disabled = false;
    }
  }
  approve.addEventListener("click", () => decide(true));
  cancel.addEventListener("click", () => decide(false));
  actions.append(approve, cancel);
  card.append(prompt, actions, status);
  log.appendChild(card);
  log.scrollTop = log.scrollHeight;
  return card;
}
async function waitForChat(requestId){
  const confirmations = new Map();
  while (true) {
    const response = await fetch(`/api/chat/status/${encodeURIComponent(requestId)}`);
    const state = await response.json();
    if (!response.ok) throw new Error(state.error || "Không lấy được trạng thái chat");
    if (state.status === "confirmation" && !confirmations.has(state.confirmation.id)) {
      confirmations.set(
        state.confirmation.id,
        addConfirmation(requestId, state.confirmation)
      );
    }
    if (state.status === "done") {
      confirmations.forEach(card => card.remove());
      return state.result;
    }
    await new Promise(resolve => setTimeout(resolve, 300));
  }
}
async function refreshProjects(){
  const response = await fetch("/projects");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Không tải được danh sách project");
  projectSelect.innerHTML = "";
  data.projects.forEach(name => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    projectSelect.appendChild(option);
  });
  projectSelect.value = data.current;
}
async function switchProject(name){
  const response = await fetch(`/projects/switch/${encodeURIComponent(name)}`, {method:"POST"});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Không chuyển được project");
  log.innerHTML = "";
  document.getElementById("usage").textContent = "";
  await refreshProjects();
  await refreshChats();
}
async function refreshChats(){
  const response = await fetch("/chats");
  const chats = await response.json();
  if (!response.ok) throw new Error(chats.error || "Không tải được danh sách chat");
  chatList.innerHTML = "";
  chats.forEach(chat => {
    const row = document.createElement("div");
    row.className = "chat-item" + (chat.current ? " active" : "");
    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "chat-open";
    openButton.textContent = chat.title || "Cuộc chat";
    openButton.title = chat.title || "Cuộc chat";
    openButton.addEventListener("click", () => openSavedChat(chat.id));
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "chat-delete";
    deleteButton.textContent = "×";
    deleteButton.title = "Xóa cuộc chat";
    deleteButton.setAttribute("aria-label", "Xóa cuộc chat");
    deleteButton.addEventListener("click", () => deleteSavedChat(chat));
    row.append(openButton, deleteButton);
    chatList.appendChild(row);
  });
}
async function openSavedChat(chatId){
  try {
    const response = await fetch(`/chats/open/${encodeURIComponent(chatId)}`, {method:"POST"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Không mở được cuộc chat");
    log.innerHTML = "";
    data.messages.forEach(message => addMsg(
      message.role === "user" ? "user" : "bot", message.text || ""
    ));
    document.getElementById("usage").textContent = "";
    refreshChats().catch(error => console.error("Không tải được danh sách chat:", error));
  } catch (error) {
    addMsg("bot", "Lỗi mở cuộc chat: " + error.message);
  }
}
async function deleteSavedChat(chat){
  if (!window.confirm(`Xóa cuộc chat "${chat.title}"?`)) return;
  try {
    const response = await fetch(`/chats/delete/${encodeURIComponent(chat.id)}`, {method:"POST"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Không xóa được cuộc chat");
    if (data.current_deleted) {
      log.innerHTML = "";
      document.getElementById("usage").textContent = "";
    }
    refreshChats().catch(error => console.error("Không tải được danh sách chat:", error));
  } catch (error) {
    addMsg("bot", "Lỗi xóa cuộc chat: " + error.message);
  }
}
async function send(){
  const text = box.value.trim();
  if (!text) return;
  const memorySince = Date.now();
  box.value = "";
  const imagePreview = pendingImagePreview;
  pendingImagePreview = null;
  addMsg("user", text, "", imagePreview);
  const wait = addMsg("bot", "Đang trả lời...");
  sendBtn.disabled = true;
  try {
    const r = await fetch("/api/chat", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({text})});
    let data = await r.json();
    if (!r.ok) throw new Error(data.error || "Yêu cầu thất bại");
    if (data.pending) data = await waitForChat(data.request_id);
    wait.remove();
    if (data.new) log.innerHTML = "";
    const reply = text === "/new" && data.new ? "" : data.reply || data.error || "(không có phản hồi)";
    const botMessage = reply ? addMsg("bot", reply, data.model || "") : null;
    if (botMessage) {
      pollMemoryRecent(memorySince, botMessage);
    }
    if (data.exports && botMessage) addExportButtons(botMessage, data.exports);
    if (data.suggest_pin && botMessage) addPinSuggestion(botMessage, data.suggest_pin);
    if (reply.includes("/bilingual")) openPanel("/bilingual", "Dịch song ngữ");
    const pdfMatch = reply.match(/\\/pdfview\\/(\\d+)\\b/);
    if (pdfMatch) openPanel(`/pdfview/${pdfMatch[1]}`, `PDF trang ${pdfMatch[1]}`);
    if (reply.includes("/vis-play")) openPanel("/vis-play", "Giải thích động");
    if (reply.includes("/vis-view")) openPanel("/vis-view", "Sơ đồ giải thích");
    if (reply.includes("Mở sơ đồ: /diagram")) openPanel(`/diagram?t=${Date.now()}`, "Sơ đồ");
    if (data.usage) document.getElementById("usage").textContent = data.usage;
    refreshChats().catch(error => console.error("Không tải được danh sách chat:", error));
  } catch (e) {
    wait.remove();
    addMsg("bot", "Lỗi kết nối tới server: " + e);
  }
  sendBtn.disabled = false;
  box.focus();
}
attachBtn.addEventListener("click", () => pdfFileInput.click());
pdfFileInput.addEventListener("change", async () => {
  const file = pdfFileInput.files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append("file", file);
  attachBtn.disabled = true;
  try {
    const response = await fetch("/upload", {method:"POST", body:formData});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "Không thể tải PDF lên");
    pendingImagePreview = data.kind === "image"
      ? "/workspace-img/" + encodeURIComponent(data.path.split("/").pop())
      : null;
    if (data.kind === "pdf") box.value = `/read ${data.path} 1`;
    else if (data.kind === "image") box.value = `/img ${data.path} `;
    else if (data.kind === "json") box.value = "/import";
    else box.value = `/file ${data.path} `;
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
  } catch (e) {
    addMsg("bot", "Lỗi tải PDF: " + e.message);
  } finally {
    pdfFileInput.value = "";
    attachBtn.disabled = false;
  }
});
sendBtn.addEventListener("click", send);
box.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey){ e.preventDefault(); send(); } });
document.getElementById("new").addEventListener("click", () => { box.value = "/new"; send(); });
document.getElementById("memory-open").addEventListener(
  "click", () => openPanel("/memory", "Bộ nhớ")
);
document.getElementById("toggle-sidebar").addEventListener("click", () => {
  document.body.classList.toggle("sidebar-collapsed");
});
projectSelect.addEventListener("change", () => {
  switchProject(projectSelect.value).catch(error => {
    addMsg("bot", "Lỗi chuyển project: " + error.message);
    refreshProjects().catch(refreshError => console.error("Không tải được danh sách project:", refreshError));
  });
});
document.getElementById("create-project").addEventListener("click", async () => {
  const enteredName = window.prompt("Tên project (chỉ chữ, số, gạch ngang hoặc gạch dưới):");
  if (enteredName === null) return;
  const name = enteredName.trim();
  if (!/^[A-Za-z0-9_-]+$/.test(name)) {
    window.alert("Tên project chỉ được chứa chữ, số, gạch ngang và gạch dưới.");
    return;
  }
  try {
    const response = await fetch("/projects/new", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Không tạo được project");
    log.innerHTML = "";
    document.getElementById("usage").textContent = "";
    await refreshProjects();
    await refreshChats();
  } catch (error) {
    addMsg("bot", "Lỗi tạo project: " + error.message);
  }
});
fetch("/api/history").then(r => r.json()).then(list => {
  list.forEach(m => addMsg(m.role === "user" ? "user" : "bot", m.text));
}).catch(error => console.error("Không tải được lịch sử chat:", error));
refreshProjects().catch(error => console.error("Không tải được danh sách project:", error));
refreshChats().catch(error => console.error("Không tải được danh sách chat:", error));
box.focus();
</script></body></html>"""


@app.get("/")
def index():
    return PAGE


MEMORY_PAGE = """<!DOCTYPE html>
<html lang="vi"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bộ nhớ</title>
<style>
body{margin:0;padding:20px;background:#f4f5f7;color:#202124;font-family:system-ui,sans-serif}
h1{font-size:1.25rem;margin:0 0 6px}
.hint,.source,.created{color:#656b75;font-size:.82rem}
#count{margin:12px 0;color:#555}
.memory{background:#fff;border:1px solid #dfe2e7;border-radius:10px;padding:12px;margin:10px 0}
.memory-text{white-space:pre-wrap;line-height:1.45}
.source{margin-top:7px;overflow-wrap:anywhere}
.created{margin-top:3px}
.actions{display:flex;gap:7px;margin-top:9px}
button{border:0;border-radius:7px;padding:7px 11px;background:#2563eb;color:#fff;cursor:pointer}
button.secondary{background:#555d6b}
form{display:flex;gap:8px;margin-top:16px}
input{flex:1;min-width:0;padding:9px;border:1px solid #c9cdd4;border-radius:7px}
#status{min-height:1.4em;margin-top:9px;color:#444;font-size:.88rem}
</style></head><body>
<h1>🧠 Bộ nhớ</h1>
<div class="hint">Thông tin được ghi nhớ trên mọi project. Tối đa 40 mục.</div>
<div id="count"></div>
<div id="items"></div>
<form id="add-form">
  <input id="new-text" maxlength="119" placeholder="Thêm thông tin cần nhớ..." required>
  <button type="submit">Thêm</button>
</form>
<div id="status" role="status"></div>
<script>
const MAX_ITEMS = __MAX_ITEMS__;
const itemsBox = document.getElementById("items");
const countBox = document.getElementById("count");
const statusBox = document.getElementById("status");
function render(items){
  itemsBox.replaceChildren();
  countBox.textContent = `${items.length}/${MAX_ITEMS} mục`;
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "hint";
    empty.textContent = "Chưa có thông tin nào được ghi nhớ.";
    itemsBox.appendChild(empty);
  }
  for (const item of items) {
    const card = document.createElement("article");
    card.className = "memory";
    const text = document.createElement("div");
    text.className = "memory-text";
    text.textContent = item.text;
    const source = document.createElement("div");
    source.className = "source";
    source.textContent = "Câu gốc: " + item.source;
    const created = document.createElement("div");
    created.className = "created";
    created.textContent = "Ngày tạo: " + item.created.slice(0, 10);
    const actions = document.createElement("div");
    actions.className = "actions";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "secondary";
    edit.textContent = "Sửa";
    edit.addEventListener("click", async () => {
      const value = window.prompt("Sửa mục ghi nhớ:", item.text);
      if (value === null) return;
      await mutate(`/memory/edit/${encodeURIComponent(item.id)}`, {text:value});
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "secondary";
    remove.textContent = "Xóa";
    remove.addEventListener("click", async () => {
      if (!window.confirm("Xóa mục ghi nhớ này?")) return;
      await mutate(`/memory/delete/${encodeURIComponent(item.id)}`, {});
    });
    actions.append(edit, remove);
    card.append(text, source, created, actions);
    itemsBox.appendChild(card);
  }
}
async function mutate(url, body){
  statusBox.textContent = "Đang lưu...";
  try {
    const response = await fetch(url, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(body)
    });
    const data = await response.json();
    if (Array.isArray(data.memories)) render(data.memories);
    if (!response.ok) throw new Error(data.error || "Không lưu được thay đổi");
    statusBox.textContent = data.message || "Đã cập nhật bộ nhớ.";
    return true;
  } catch (error) {
    statusBox.textContent = error.message;
    return false;
  }
}
document.getElementById("add-form").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.getElementById("new-text");
  if (await mutate("/memory/add", {text:input.value})) input.value = "";
});
render(__MEMORIES__);
</script></body></html>"""


@app.get("/memory")
def memory_panel():
    memories = list_memories()
    payload = json.dumps(memories, ensure_ascii=True).replace("</", "<\\/")
    page = MEMORY_PAGE.replace("__MAX_ITEMS__", str(MAX_MEMORIES))
    page = page.replace("__MEMORIES__", payload)
    return app.response_class(page, mimetype="text/html")


@app.get("/memory/recent")
def memory_recent():
    raw_since = request.args.get("since", "0")
    try:
        since = int(raw_since)
    except ValueError:
        return jsonify(error="Tham số since phải là timestamp milliseconds."), 400
    with memory_recent_events_lock:
        events = [
            event for event in memory_recent_events
            if event["timestamp"] >= since
        ]
    return jsonify(events=events)


@app.post("/memory/add")
def memory_add():
    data = request.get_json(silent=True)
    text = data.get("text") if isinstance(data, dict) else None
    try:
        item, reason = add_memory(text, "người dùng nhập tay")
    except ValueError as error:
        return jsonify(error=str(error)), 400
    if reason:
        message = (
            "Bộ nhớ đã đủ 40 mục; hãy xóa một mục trước khi thêm."
            if reason == "full"
            else "Thông tin này đã có trong bộ nhớ."
        )
        return jsonify(error=message, memories=list_memories()), 409
    _record_memory_event("remembered", item["text"])
    return jsonify(message="Đã thêm vào bộ nhớ.", memories=list_memories()), 201


@app.post("/memory/edit/<memory_id>")
def memory_edit(memory_id):
    data = request.get_json(silent=True)
    text = data.get("text") if isinstance(data, dict) else None
    try:
        item, reason = edit_memory(memory_id, text)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    if reason:
        message = (
            "Không tìm thấy mục ghi nhớ."
            if reason == "missing"
            else "Thông tin này đã có trong bộ nhớ."
        )
        return jsonify(error=message, memories=list_memories()), 404 if reason == "missing" else 409
    _record_memory_event("remembered", item["text"])
    return jsonify(message="Đã sửa mục ghi nhớ.", memories=list_memories())


@app.post("/memory/delete/<memory_id>")
def memory_delete(memory_id):
    item = next(
        (entry for entry in list_memories() if entry["id"] == memory_id),
        None,
    )
    if item is None or not delete_memory(memory_id):
        return jsonify(error="Không tìm thấy mục ghi nhớ.", memories=list_memories()), 404
    _record_memory_event("forgot", item["text"])
    return jsonify(message="Đã xóa mục ghi nhớ.", memories=list_memories())


@app.get("/bilingual")
def bilingual():
    return send_from_directory(str(get_workspace_dir()), "bilingual.html")


@app.get("/pdfview/<int:page_no>")
def pdf_view(page_no):
    return send_from_directory(str(get_workspace_dir()), f"pdf_trang{page_no}.html")


@app.get("/vis-play")
def vis_play():
    return send_from_directory(str(get_workspace_dir()), "vis_play.html")


@app.get("/vis-view")
def vis_view():
    return send_from_directory(str(get_workspace_dir()), "vis.html")


@app.get("/diagram")
def diagram():
    diagram_path = get_workspace_dir() / "diagram.md"
    diagram_text = ""
    if diagram_path.is_file() and not diagram_path.is_symlink():
        diagram_text = diagram_path.read_text(encoding="utf-8")
    match = re.search(r"```mermaid\s*\n(.*?)```", diagram_text, re.DOTALL)
    if match is None:
        toolbar = ""
        content = '<main class="diagram-viewport"><p>Chưa có sơ đồ</p></main>'
    else:
        toolbar = (
            '<div class="zoom-controls">'
            '<button type="button" data-zoom="in" aria-label="Phóng to">+</button>'
            '<button type="button" data-zoom="out" aria-label="Thu nhỏ">−</button>'
            '<button type="button" data-zoom="reset">Đặt lại</button>'
            "</div>"
        )
        content = (
            '<main class="diagram-viewport"><div class="diagram-canvas" '
            'id="diagram-canvas"><pre class="mermaid">'
            + html.escape(match.group(1))
            + "</pre></div></main>"
        )
    return (
        "<!DOCTYPE html><html lang=\"vi\"><head><meta charset=\"UTF-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Sơ đồ</title><style>"
        "*{box-sizing:border-box}body{margin:0;padding:12px;height:100vh;"
        "display:flex;flex-direction:column;gap:8px;background:#1b1c1f;color:#e8e8ea;"
        "font-family:system-ui,sans-serif}.zoom-controls{display:flex;gap:6px}"
        "button{padding:4px 9px;border:0;border-radius:6px;background:#2e3036;"
        "color:#e8e8ea;cursor:pointer}.diagram-viewport{flex:1;min-height:0;"
        "overflow:auto}.diagram-canvas{width:max-content;min-width:100%;"
        "transform:scale(1);transform-origin:top left}.diagram-canvas svg{max-width:none}"
        "pre{white-space:pre-wrap}.mermaid{display:flex;justify-content:center}"
        "</style><script src=\"https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js\">"
        "</script><script>mermaid.initialize({startOnLoad:true, theme:'dark'});</script>"
        "</head><body>" + toolbar + content
        + '<script>const diagramCanvas=document.getElementById("diagram-canvas");'
        "let diagramZoom=1;"
        'document.querySelectorAll("[data-zoom]").forEach(button=>button.addEventListener("click",()=>{'
        'if(!diagramCanvas)return;const action=button.dataset.zoom;'
        'diagramZoom=action==="reset"?1:Math.max(0.25,Math.min(3,diagramZoom+(action==="in"?0.2:-0.2)));'
        'diagramCanvas.style.transform=`scale(${diagramZoom})`;}));</script>'
        "</body></html>"
    )


@app.get("/download/<path:filename>")
def download_export(filename):
    if ".." in filename or "/" in filename or "\\" in filename:
        return "", 404
    return send_from_directory(str(EXPORT_DIR), filename, as_attachment=True)


@app.get("/files/<path:filename>")
def download_workspace_file(filename):
    if ".." in filename or "/" in filename or "\\" in filename:
        return "", 404
    if os.path.splitext(filename)[1].lower() not in DOWNLOADABLE_WORKSPACE_EXTENSIONS:
        return "", 404
    target = get_workspace_dir() / filename
    if target.is_symlink() or not target.is_file():
        return "", 404
    return send_from_directory(str(get_workspace_dir()), filename, as_attachment=True)


@app.post("/upload")
def upload():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify(error="Chọn một file để tải lên."), 400
    original_name = uploaded.filename.replace("\\", "/").rsplit("/", 1)[-1]
    lowered_name = original_name.lower()
    if lowered_name == ".env" or lowered_name.startswith(".env.") or any(
        blocked in lowered_name for blocked in ("secret", "key")
    ):
        return jsonify(error="Không chấp nhận file .env hoặc tên có secret/key."), 400
    extension = os.path.splitext(original_name)[1].lower()
    if extension not in ALLOWED_UPLOAD_EXTENSIONS:
        return jsonify(error="Loại file không được hỗ trợ."), 400
    filename = secure_filename(uploaded.filename)
    if not filename or os.path.splitext(filename)[1].lower() != extension:
        return jsonify(error="Tên file không hợp lệ."), 400
    content = uploaded.stream.read(MAX_UPLOAD_SIZE + 1)
    if len(content) > MAX_UPLOAD_SIZE:
        return jsonify(error="File vượt quá giới hạn 30 MB."), 413
    if extension == ".json":
        EXPORT_DIR.mkdir(exist_ok=True)
        (EXPORT_DIR / filename).write_bytes(content)
        return jsonify(path=filename, kind="json")
    workspace_dir = get_workspace_dir()
    (workspace_dir / filename).write_bytes(content)
    kind = "pdf" if extension == ".pdf" else (
        "image" if extension in IMAGE_UPLOAD_EXTENSIONS else "text")
    return jsonify(path=f"workspace/{filename}", kind=kind)


@app.get("/workspace-img/<path:filename>")
def workspace_image(filename):
    if ".." in filename or "/" in filename or "\\" in filename:
        return "", 404
    if os.path.splitext(filename)[1].lower() not in IMAGE_UPLOAD_EXTENSIONS:
        return "", 404
    return send_from_directory(str(get_workspace_dir()), filename)


@app.get("/api/history")
def history():
    with lock:
        return jsonify(messages)


@app.get("/chats")
def chats():
    with lock:
        return jsonify(_list_chats())


@app.get("/projects")
def projects():
    with lock:
        return jsonify(projects=_list_projects(), current=current_project)


@app.post("/projects/new")
def new_project():
    data = request.get_json(silent=True)
    name = data.get("name") if isinstance(data, dict) else None
    if not is_valid_project_name(name):
        return jsonify(error="Tên project chỉ được chứa chữ, số, gạch ngang và gạch dưới."), 400
    with lock:
        project_dir = _project_dir(name)
        if project_dir.exists():
            return jsonify(error="Project đã tồn tại."), 409
        _ensure_project_layout(name)
        _switch_project(name)
        return jsonify(projects=_list_projects(), current=current_project), 201


@app.post("/projects/switch/<name>")
def switch_project(name):
    if not is_valid_project_name(name):
        return jsonify(error="Tên project không hợp lệ."), 400
    with lock:
        if name not in _list_projects():
            return jsonify(error="Không tìm thấy project."), 404
        _switch_project(name)
        return jsonify(projects=_list_projects(), current=current_project)


@app.post("/chats/open/<chat_id>")
def open_chat(chat_id):
    global current_chat_id, current_extracted_upto, messages
    path = _chat_path(chat_id)
    if path is None:
        return jsonify(error="Không tìm thấy cuộc chat."), 404
    with lock:
        if path.is_symlink() or not path.is_file():
            return jsonify(error="Không tìm thấy cuộc chat."), 404
        _save_current_chat()
        if chat_id != current_chat_id:
            _schedule_memory_extraction(
                current_project, current_chat_id, messages, current_extracted_upto)
        chat_data = _read_chat(path)
        messages = chat_data["messages"]
        current_chat_id = chat_id
        current_extracted_upto = chat_data.get("extracted_upto", 0)
        save_history(messages)
        opened_messages = messages[:]
    return jsonify(messages=opened_messages, current_id=chat_id)


@app.post("/chats/delete/<chat_id>")
def delete_chat(chat_id):
    global current_chat_id, current_extracted_upto, messages
    path = _chat_path(chat_id)
    if path is None:
        return jsonify(error="Không tìm thấy cuộc chat."), 404
    with lock:
        if path.is_symlink() or not path.is_file():
            return jsonify(error="Không tìm thấy cuộc chat."), 404
        path.unlink()
        deleted_current = current_chat_id == chat_id
        if deleted_current:
            current_chat_id = None
            current_extracted_upto = 0
            messages = []
            save_history(messages)
    return jsonify(ok=True, current_deleted=deleted_current)


def _process_chat_message(text):
    global current_chat_id, current_extracted_upto, messages
    if text == "/new":
        _save_current_chat()
        _schedule_memory_extraction(
            current_project, current_chat_id, messages, current_extracted_upto)
        messages = []
        current_chat_id = None
        current_extracted_upto = 0
        save_history(messages)
        return {"reply": "Đã bắt đầu cuộc chat mới.", "model": "", "new": True}
    if text == "/nho" or text.startswith("/nho "):
        memory_text = text[4:].strip()
        if not memory_text:
            return {"reply": "Dùng: /nho nội dung cần ghi nhớ", "model": ""}
        try:
            _, reason = add_memory(memory_text, "người dùng nhập tay")
        except ValueError as error:
            return {"reply": str(error), "model": ""}
        if reason == "full":
            reply = "Bộ nhớ đã đủ 40 mục; hãy xóa một mục trước khi thêm."
        elif reason == "duplicate":
            reply = "Thông tin này đã có trong bộ nhớ."
        else:
            reply = "Đã ghi nhớ thông tin trên mọi project."
            _record_memory_event("remembered", memory_text)
        return {"reply": reply, "model": ""}
    if text == "/bonho":
        saved_memories = list_memories()
        if not saved_memories:
            return {"reply": "Chưa có mục nào trong bộ nhớ.", "model": ""}
        lines = ["Bộ nhớ toàn cục:"]
        for number, item in enumerate(saved_memories, 1):
            lines.append(f"{number}. {item['text']}")
            lines.append(f"   Câu gốc: {item['source']}")
        return {"reply": "\n".join(lines), "model": ""}
    if text == "/quen" or text.startswith("/quen "):
        match = re.fullmatch(r"/quen\s+(\d+)", text)
        if not match:
            return {"reply": "Dùng: /quen số_thứ_tự", "model": ""}
        deleted = delete_memory_by_number(int(match.group(1)))
        if deleted is None:
            return {"reply": "Không tìm thấy mục bộ nhớ theo số đó.", "model": ""}
        _record_memory_event("forgot", deleted["text"])
        return {"reply": f"Đã quên: {deleted['text']}", "model": ""}
    if text.startswith("/export"):
        try:
            json_path, _ = export_chat(messages, text[7:])
        except Exception as e:
            return {"reply": f"Lỗi xuất lịch sử: {e}", "model": ""}
        filename = json_path.name
        return {
            "reply": f"Đã xuất {filename}. Tải xuống: /download/{filename}",
            "model": "",
        }
    if text == "/import" or text.startswith("/import "):
        name = text[7:].strip()
        if not name:
            exports = [
                export_name for export_name in list_exports()
                if (EXPORT_DIR / f"{export_name}.json").is_file()
            ]
            exports.sort(
                key=lambda export_name: (
                    EXPORT_DIR / f"{export_name}.json"
                ).stat().st_mtime,
                reverse=True,
            )
            return {
                "reply": "Chọn bản xuất để nạp:" if exports else "Chưa có bản xuất nào.",
                "exports": exports[:10],
                "model": "",
            }
        try:
            imported_messages, error = import_chat(name)
        except Exception as e:
            return {"reply": f"Lỗi nạp lịch sử: {e}", "model": ""}
        if error:
            return {"reply": error, "model": ""}
        messages = imported_messages
        current_extracted_upto = 0
        save_history(messages)
        return {
            "reply": f"Đã nạp {len(messages)} tin nhắn từ '{name}'.",
            "model": "",
            "new": True,
            "history": messages,
        }
    if text == "/pins" or text.startswith("/pin ") or text.startswith("/unpin"):
        pins = load_pins(current_project)
        if text == "/pins":
            reply = format_pins(pins)
        elif text.startswith("/unpin"):
            reply = remove_pin(pins, text[6:].strip(), current_project)
        else:
            reply = add_pin(pins, text[5:], current_project)
        return {"reply": reply, "model": ""}
    if text == "/en" or text.startswith("/en "):
        sentence = text[3:].strip()
        if not sentence:
            return {"reply": "Dùng: /en câu cần dịch", "model": ""}
        try:
            reply, model_key, _ = translate(
                gemini_client, openai_clients, sentence, open_browser=False)
        except Exception as e:
            return {"reply": f"Lỗi dịch: {e}", "model": ""}
        reply = reply.replace(
            f"(Đã mở {get_workspace_dir() / 'bilingual.html'})",
            "Đã dịch xong. Mở bản song ngữ: /bilingual",
        )
        return {"reply": reply, "model": model_key}
    if text == "/play" or text.startswith("/play "):
        play_content = text[5:].strip()
        if not play_content:
            return {"reply": "Dùng: /play chủ đề cần giải thích", "model": ""}
        try:
            raw_reply, model_key, _ = play(
                gemini_client, openai_clients, play_content, open_browser=False)
        except Exception as e:
            return {"reply": f"Lỗi tạo video: {e}", "model": ""}
        scenes = parse_scenes(raw_reply)
        if scenes:
            title = " ".join(scenes[0]["title"].split())[:80]
            reply = f"{title} — {len(scenes)} cảnh.\nMở hoạt hình: /vis-play"
        else:
            reply = "Giải thích động.\nMở hoạt hình: /vis-play"
        return {
            "reply": reply,
            "model": model_key,
        }
    if text == "/vis" or text.startswith("/vis "):
        vis_content = text[4:].strip()
        if not vis_content:
            return {"reply": "Dùng: /vis câu hỏi hoặc đoạn code", "model": ""}
        try:
            _, model_key, _ = visualize(
                gemini_client, openai_clients, vis_content, open_browser=False)
        except Exception as e:
            return {"reply": f"Lỗi trực quan hóa: {e}", "model": ""}
        return {
            "reply": "Sơ đồ giải thích đã sẵn sàng.\nMở sơ đồ: /vis-view",
            "model": model_key,
        }
    if text == "/read" or text.startswith("/read "):
        args = text[5:].strip()
        read_command = parse_read_command(args)
        if read_command is None:
            return {"reply": "Dùng: /read đường_dẫn.pdf [số trang]", "model": ""}
        args, page_no = read_command
        try:
            reply, model_key = read_pdf_page(
                gemini_client, openai_clients, args, page_no, open_browser=False)
        except Exception as e:
            return {"reply": f"Lỗi đọc PDF: {e}", "model": ""}
        pdf_html_path = f"workspace/pdf_trang{page_no}.html"
        reply = reply.replace(pdf_html_path, f"/pdfview/{page_no}")
        return {"reply": reply, "model": model_key}
    if text == "/img" or text.startswith("/img "):
        rest = text[4:].strip()
        if rest.startswith('"') and '"' in rest[1:]:
            path, _, question = rest[1:].partition('"')
        else:
            path, _, question = rest.partition(" ")
        path, question = path.strip(), question.strip()
        if not path:
            return {"reply": "Dùng: /img đường_dẫn_ảnh câu hỏi", "model": ""}
        try:
            reply, model_key, meta = ask_image(
                gemini_client, openai_clients, path, question)
        except Exception as e:
            return {"reply": f"Lỗi đọc ảnh: {e}", "model": ""}
        if not model_key:
            return {"reply": reply, "model": ""}
        record_usage(usage, model_key, meta)
        return {
            "reply": reply,
            "model": model_key,
            "usage": format_usage(usage, model_key),
        }
    if text == "/file" or text.startswith("/file "):
        rest = text[5:].strip()
        if rest.startswith('"') and '"' in rest[1:]:
            path, _, question = rest[1:].partition('"')
        else:
            path, _, question = rest.partition(" ")
        path, question = path.strip(), question.strip()
        if not path:
            return {"reply": "Dùng: /file đường_dẫn câu hỏi", "model": ""}
        try:
            reply, model_key, meta, note = ask_file(
                gemini_client, openai_clients, path, question)
        except Exception as e:
            return {"reply": f"Lỗi đọc file: {e}", "model": ""}
        if not model_key:
            return {"reply": reply, "model": ""}
        record_usage(usage, model_key, meta)
        return {
            "reply": f"{reply}\n{note}",
            "model": model_key,
            "usage": format_usage(usage, model_key),
        }

    question, prompt, sources = text, text, []
    if text.startswith("/web "):
        question = text[5:].strip()
        try:
            results = web_search(question)
        except Exception as e:
            return {"reply": f"Lỗi tìm web: {e}", "model": ""}
        if not results:
            return {"reply": "Không tìm thấy kết quả nào.", "model": ""}
        prompt = build_prompt(question, results)
        sources = [(r.get("title", ""), r.get("href", "")) for r in results]
    try:
        answer, model_key, meta = ask(
            gemini_client, openai_clients, messages, prompt, use_history=not sources)
    except Exception as e:
        return {"reply": f"Lỗi: tất cả model đều thất bại. {e}", "model": ""}
    saved = answer
    if sources:
        saved += "\n\nNguồn:\n" + "\n".join(
            f"[{i}] {t}: {u}" for i, (t, u) in enumerate(sources, 1))
    messages.append({"role": "user", "text": question})
    messages.append({"role": "model", "text": saved})
    save_history(messages)
    record_usage(usage, model_key, meta)
    result = {"reply": saved, "model": model_key, "usage": format_usage(usage, model_key)}
    if looks_personal(text):
        result["suggest_pin"] = text
    return result


def _run_chat_job(job_id, text):
    global active_job_id
    confirm_context.job_id = job_id
    try:
        with lock:
            messages_before = messages[:]
            workspace_before = _workspace_file_mtimes()
            try:
                result = _process_chat_message(text)
            except Exception as e:
                result = {"reply": f"Lỗi xử lý: {e}", "model": ""}
            workspace_after = _workspace_file_mtimes()
            created_files = sorted(
                filename
                for filename, modified_at in workspace_after.items()
                if workspace_before.get(filename) != modified_at
            )
            if created_files and isinstance(result.get("reply"), str):
                result["reply"] += "\n\n" + "\n".join(
                    f"📄 File đã tạo: {filename} — tải về: /files/{quote(filename, safe='')}"
                    for filename in created_files
                )
            if (
                "diagram.md" in workspace_after
                and workspace_before.get("diagram.md") != workspace_after["diagram.md"]
                and isinstance(result.get("reply"), str)
            ):
                result["reply"] += "\n\nMở sơ đồ: /diagram"
            _save_current_chat()
            if (
                not result.get("new")
                and len(messages) == len(messages_before) + 2
                and messages[:-2] == messages_before
                and messages[-2].get("role") == "user"
                and messages[-1].get("role") == "model"
            ):
                user_index = sum(
                    1 for item in messages
                    if isinstance(item, dict) and item.get("role") == "user"
                )
                _schedule_memory_message(
                    current_project,
                    current_chat_id,
                    messages[-2]["text"],
                    user_index,
                )
    except Exception as e:
        result = {"reply": f"Lỗi xử lý: {e}", "model": ""}
    finally:
        del confirm_context.job_id
        with chat_jobs_lock:
            job = chat_jobs.get(job_id)
            if job is not None:
                job["result"] = result
                job["status"] = "done"
                job["confirmation"] = None
            if active_job_id == job_id:
                active_job_id = None


@app.post("/api/chat")
def chat():
    global active_job_id
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify(error="Tin nhắn trống"), 400
    with chat_jobs_lock:
        if active_job_id is not None:
            return jsonify(error="Yêu cầu trước vẫn đang xử lý."), 409
        now = time.monotonic()
        for old_id, old_job in list(chat_jobs.items()):
            if old_job["status"] == "done" and now - old_job["created_at"] > 600:
                del chat_jobs[old_id]
        job_id = uuid.uuid4().hex
        chat_jobs[job_id] = {
            "created_at": now,
            "status": "running",
            "confirmation": None,
            "confirmation_id": 0,
            "deadline": None,
            "decision": None,
            "decision_event": threading.Event(),
            "result": None,
        }
        active_job_id = job_id
        threading.Thread(
            target=_run_chat_job, args=(job_id, text), daemon=True).start()
    response = {"pending": True, "request_id": job_id}
    if looks_personal(text):
        response["suggest_pin"] = text
    return jsonify(response), 202


@app.get("/api/chat/status/<request_id>")
def chat_status(request_id):
    with chat_jobs_lock:
        job = chat_jobs.get(request_id)
        if job is None:
            return jsonify(error="Không tìm thấy yêu cầu chat."), 404
        response = {"status": job["status"]}
        if job["status"] == "confirmation":
            response["confirmation"] = job["confirmation"]
        elif job["status"] == "done":
            response["result"] = job["result"]
        return jsonify(response)


@app.post("/confirm")
def confirm():
    data = request.get_json(silent=True) or {}
    request_id = data.get("request_id")
    confirmation_id = data.get("confirmation_id")
    approved = data.get("approved")
    if not isinstance(request_id, str) or type(confirmation_id) is not int:
        return jsonify(error="Thiếu mã yêu cầu xác nhận."), 400
    if not isinstance(approved, bool):
        return jsonify(error="Quyết định xác nhận không hợp lệ."), 400
    with chat_jobs_lock:
        job = chat_jobs.get(request_id)
        if (
            job is None
            or job["status"] != "confirmation"
            or job["confirmation_id"] != confirmation_id
            or job["deadline"] is None
            or time.monotonic() >= job["deadline"]
            or job["decision_event"].is_set()
        ):
            return jsonify(error="Yêu cầu xác nhận đã hết hạn hoặc không còn hiệu lực."), 409
        job["decision"] = approved
        job["decision_event"].set()
    return jsonify(ok=True)


if __name__ == "__main__":
    examples = (
        ("Tôi đang làm đồ án chatbot cá nhân bằng Python", True),
        ("tên tôi là Hải", True),
        ("TCP là gì?", False),
        ("hello", False),
    )
    for text, expected in examples:
        print(f"{text}: {looks_personal(text)} (expected {expected})")
    url = "http://127.0.0.1:5000"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=5000)