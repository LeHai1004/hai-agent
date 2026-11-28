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
from artifacts import (
    ArtifactVersionConflict,
    apply_patch as apply_artifact_patch,
    get_artifact,
    list_artifacts,
    split_markdown_blocks,
)
from chat_flow import (
    run_chat_turn,
    run_chat_turn_stream,
    schedule_memory_scan,
    schedule_thread_summary,
)
from tools.artifact import set_artifact_handler
from config import MEMORY_BACKGROUND_SCAN
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
from flask import Flask, Response, jsonify, request, send_from_directory
from flask import stream_with_context
from transfer import EXPORT_DIR, export_chat, import_chat, list_exports
from google import genai
from google.genai import types
from openai import OpenAI
from werkzeug.utils import secure_filename

from history import load_history_state, save_history, update_history_summary
from llm import ask, ask_stream
from memory import (
    InvalidModelJSON,
    MAX_MEMORIES,
    add_memory,
    add_memories,
    build_deletion_prompt,
    build_extraction_prompt,
    delete_memory,
    edit_memory,
    extract_memories as validate_extracted_memories,
    has_memory_hint,
    list_memories,
    memory_intent,
    memory_log,
    parse_delete_ids,
    request_json_model,
    save_and_verify,
    delete_and_verify,
)
from filechat import ask_file
from img import ask_image
from pdfread import parse_read_command, read_pdf_page
from search import build_prompt, web_search
from translate import translate
from usage import format_usage, load_usage, record_usage
from visual import parse_scenes, play, visualize
from storage import write_json

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
chat_jobs_condition = threading.Condition(chat_jobs_lock)
chat_jobs = {}
active_job_id = None
CHAT_JOB_TTL = 600
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


def _append_chat_event_locked(job, event):
    payload = dict(event)
    event_id = job["last_event_id"] + 1
    job["last_event_id"] = event_id
    job["events"].append({"id": event_id, "data": payload})
    if payload["type"] == "memory":
        job["memory_event_sent"] = True
    if payload["type"] == "final":
        job["final_event_sent"] = True
    return event_id


def _publish_chat_event(job_id, event):
    with chat_jobs_condition:
        job = chat_jobs.get(job_id)
        if job is None:
            return None
        event_id = _append_chat_event_locked(job, event)
        chat_jobs_condition.notify_all()
        return event_id


def _prune_chat_jobs_locked(now):
    for old_id, old_job in list(chat_jobs.items()):
        if (
            old_job["status"] == "done"
            and now - old_job["created_at"] > CHAT_JOB_TTL
        ):
            del chat_jobs[old_id]


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
    global current_project, current_chat_id, current_extracted_upto
    global current_summary, current_summary_upto, messages
    if name == current_project:
        return
    _save_current_chat()
    messages = []
    current_chat_id = None
    current_extracted_upto = 0
    current_summary = ""
    current_summary_upto = 0
    save_history(messages, summary=current_summary, summary_upto=current_summary_upto)
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
        or not isinstance(chat.get("summary", ""), str)
        or type(chat.get("summary_upto", 0)) is not int
        or chat.get("summary_upto", 0) < 0
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
        "summary": current_summary,
        "summary_upto": current_summary_upto,
    }
    write_json(path, chat)


def _save_chat_summary(project, chat_id, summary, summary_upto):
    global current_summary, current_summary_upto
    with lock:
        path = _project_dir(project) / "chats" / f"{chat_id}.json"
        if path.is_symlink() or not path.is_file():
            memory_log(f"[memory] lỗi lưu tóm tắt: không tìm thấy chat {chat_id}")
            return False
        chat = _read_chat(path)
        if summary_upto <= chat.get("summary_upto", 0):
            return False
        chat["summary"] = summary
        chat["summary_upto"] = summary_upto
        write_json(path, chat)
        if current_project == project and current_chat_id == chat_id:
            current_summary = summary
            current_summary_upto = summary_upto
            update_history_summary(summary, summary_upto)
        return True


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
        if intent == "forget":
            def record_usage_safely(model_key, meta):
                with lock:
                    record_usage(model_key, meta)

            reply, model_key, meta = request_json_model(
                gemini_client,
                openai_clients,
                [],
                prompt,
                ask,
                record_usage_safely,
                route_text="chọn mục bộ nhớ cần xóa",
            )
        else:
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
            with lock:
                record_usage(model_key, meta)
        memory_log(f"[memory] model trả: {str(reply)[:600]!r}")
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
    except InvalidModelJSON:
        memory_log("[memory] bỏ xóa: model trả JSON không hợp lệ")
    except Exception as error:
        safe_error = str(error).encode("ascii", "backslashreplace").decode("ascii")
        memory_log(f"[memory] lỗi: {safe_error}")
    finally:
        with memory_extraction_jobs_lock:
            memory_extraction_jobs.discard(job_key)


def _schedule_memory_message(project, chat_id, user_message, message_index):
    if not MEMORY_BACKGROUND_SCAN:
        return
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
    if not MEMORY_BACKGROUND_SCAN:
        return
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
messages, current_summary, current_summary_upto = load_history_state()
if messages:
    saved_chat = _find_saved_chat(messages)
    if saved_chat is not None:
        current_chat_id = saved_chat[0]
        current_extracted_upto = saved_chat[1].get("extracted_upto", 0)
        current_summary = saved_chat[1].get("summary", "")
        current_summary_upto = saved_chat[1].get("summary_upto", 0)
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
.artifact-card{display:block;margin-top:8px;padding:8px 12px;border:1px solid #3a3d45;border-radius:8px;background:#24262b;color:#9cc5ff;text-align:left}
.artifact-card:hover{background:#2e3036}
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
const confirmationCards = new Map();
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
function addArtifactCard(container, id, title){
  const card = document.createElement("button");
  card.type = "button";
  card.className = "artifact-card";
  card.dataset.artifactId = id;
  card.textContent = "📄 " + title;
  card.addEventListener("click", () => {
    openPanel(`/artifact-view/${encodeURIComponent(id)}`, title);
  });
  container.appendChild(card);
  return card;
}
function artifactReferences(text){
  const pattern = /Artifact: ([\\s\\S]*?) \\(id: ([0-9a-f]{32})\\)/g;
  const references = Array.from(text.matchAll(pattern));
  if (!references.length || references.map(match => match[0]).join("\\n") !== text) {
    return null;
  }
  return references.map(match => ({id: match[2], title: match[1]}));
}
function renderBotReply(container, text){
  container.replaceChildren();
  const references = artifactReferences(text);
  if (references) {
    const seenIds = new Set();
    references.forEach(reference => {
      if (seenIds.has(reference.id)) return;
      seenIds.add(reference.id);
      addArtifactCard(container, reference.id, reference.title);
    });
  } else {
    container.innerHTML = render(text);
  }
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
  } else renderBotReply(d, text);
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
      const shouldScroll = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
      for (const event of data.events || []) {
        if (event.action !== "scanned" || seen.has(event.id)) continue;
        seen.add(event.id);
        const note = document.createElement("div");
        note.className = "memory-result";
        note.textContent = "🧠 Đã cập nhật bộ nhớ: " + event.text;
        botMessage.appendChild(note);
      }
      if (shouldScroll) log.scrollTop = log.scrollHeight;
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
  const confirmationKey = `${requestId}:${confirmation.id}`;
  const existing = confirmationCards.get(confirmationKey);
  if (existing) return existing;
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
  confirmationCards.set(confirmationKey, card);
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
      confirmations.forEach((card, confirmationId) => {
        card.remove();
        const key = `${requestId}:${confirmationId}`;
        if (confirmationCards.get(key) === card) {
          confirmationCards.delete(key);
        }
      });
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
  let autoScroll = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
  const previousScrollTop = log.scrollTop;
  box.value = "";
  const imagePreview = pendingImagePreview;
  pendingImagePreview = null;
  addMsg("user", text, "", imagePreview);
  const wait = addMsg("bot", "");
  const streamText = document.createTextNode("Đang trả lời...");
  wait.appendChild(streamText);
  if (!autoScroll) log.scrollTop = previousScrollTop;
  const trackScroll = () => {
    autoScroll = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
  };
  log.addEventListener("scroll", trackScroll);
  const keepScrolled = () => {
    if (autoScroll) log.scrollTop = log.scrollHeight;
  };
  sendBtn.disabled = true;
  try {
    const r = await fetch("/api/chat", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({text})});
    const accepted = await r.json();
    if (!r.ok) throw new Error(accepted.error || "Yêu cầu thất bại");
    let data = accepted;
    let streamedFinal = null;
    let streamError = null;
    const artifactCards = [];
    const artifactIds = new Set();
    const restoreArtifactCards = () => {
      const present = new Set(Array.from(
        wait.querySelectorAll(".artifact-card")
      ).map(card => card.dataset.artifactId));
      artifactCards.forEach(card => {
        if (present.has(card.dataset.artifactId)) return;
        wait.appendChild(card);
        present.add(card.dataset.artifactId);
      });
    };
    if (accepted.pending && accepted.request_id) {
      const requestId = accepted.request_id;
      const polledResult = waitForChat(requestId).catch(error => ({pollError:error}));
      if (typeof EventSource === "function") {
        let source;
        let connectionErrors = 0;
        const streamOutcome = await new Promise(resolve => {
          let settled = false;
          const finishStream = outcome => {
            if (settled) return;
            settled = true;
            if (source) source.close();
            resolve(outcome);
          };
          try {
            source = new EventSource(`/events/${encodeURIComponent(requestId)}`);
          } catch (error) {
            console.warn("Không mở được SSE; dùng polling:", error);
            finishStream("fallback");
            return;
          }
          source.onopen = () => { connectionErrors = 0; };
          source.addEventListener("token", event => {
            try {
              const payload = JSON.parse(event.data);
              if (streamText.textContent === "Đang trả lời...") {
                streamText.textContent = "";
              }
              const status = wait.querySelector(".stream-tool-status");
              if (status) status.remove();
              streamText.textContent += payload.text || "";
              keepScrolled();
            } catch (error) {
              console.error("SSE token không hợp lệ:", error);
            }
          });
          source.addEventListener("tool", event => {
            try {
              const payload = JSON.parse(event.data);
              let status = wait.querySelector(".stream-tool-status");
              if (!status) {
                status = document.createElement("div");
                status.className = "meta stream-tool-status";
                wait.appendChild(status);
              }
              status.textContent = `đang dùng ${payload.name}…`;
              keepScrolled();
            } catch (error) {
              console.error("SSE tool event không hợp lệ:", error);
            }
          });
          source.addEventListener("memory", event => {
            try {
              const payload = JSON.parse(event.data);
              const note = document.createElement("div");
              note.className = "memory-result";
              note.textContent = "🧠 Đã cập nhật bộ nhớ: " + payload.text;
              wait.appendChild(note);
              keepScrolled();
            } catch (error) {
              console.error("SSE memory event không hợp lệ:", error);
            }
          });
          source.addEventListener("artifact", event => {
            try {
              const payload = JSON.parse(event.data);
              if (!payload.id || artifactIds.has(payload.id)) return;
              artifactIds.add(payload.id);
              const card = addArtifactCard(wait, payload.id, payload.title || "Artifact");
              artifactCards.push(card);
              openPanel(
                `/artifact-view/${encodeURIComponent(payload.id)}`,
                payload.title || "Artifact"
              );
              keepScrolled();
            } catch (error) {
              console.error("SSE artifact không hợp lệ:", error);
            }
          });
          source.addEventListener("final", event => {
            try {
              streamedFinal = JSON.parse(event.data);
              const memoryNotes = Array.from(
                wait.querySelectorAll(".memory-result")
              );
              renderBotReply(wait, streamedFinal.reply || "");
              if (streamedFinal.model) {
                const meta = document.createElement("div");
                meta.className = "meta";
                meta.textContent = streamedFinal.model;
                wait.appendChild(meta);
              }
              memoryNotes.forEach(note => wait.appendChild(note));
              restoreArtifactCards();
              keepScrolled();
            } catch (error) {
              console.error("SSE final event không hợp lệ:", error);
            }
          });
          source.addEventListener("error", event => {
            if (!event.data) return;
            try {
              const payload = JSON.parse(event.data);
              streamError = payload.message || "Luồng trả lời gặp lỗi.";
              finishStream("error");
            } catch (error) {
              console.error("SSE error event không hợp lệ:", error);
            }
          });
          source.addEventListener("done", () => {
            finishStream("done");
            sendBtn.disabled = false;
            box.focus();
          });
          source.onerror = () => {
            connectionErrors += 1;
            if (connectionErrors >= 3) finishStream("fallback");
          };
        });
        if (streamOutcome === "fallback") {
          console.warn("SSE reconnect failed; using chat status polling.");
        }
      } else {
        console.warn("EventSource unavailable; using chat status polling.");
      }
      const polled = await polledResult;
      if (polled && polled.pollError) throw polled.pollError;
      data = polled;
      if (streamedFinal) {
        data.reply = streamedFinal.reply || data.reply;
        data.model = streamedFinal.model || data.model;
        if (typeof polled.reply === "string" && polled.reply !== streamedFinal.reply) {
          data.reply = polled.reply;
          const memoryNotes = Array.from(wait.querySelectorAll(".memory-result"));
          renderBotReply(wait, data.reply);
          if (data.model) {
            const meta = document.createElement("div");
            meta.className = "meta";
            meta.textContent = data.model;
            wait.appendChild(meta);
          }
          memoryNotes.forEach(note => wait.appendChild(note));
          restoreArtifactCards();
          keepScrolled();
        }
      }
      if (streamError && !streamedFinal) {
        const partial = streamText.textContent === "Đang trả lời..."
          ? ""
          : streamText.textContent;
        if (!partial && data.reply) {
          streamText.textContent = data.reply;
        }
        const errorNote = document.createElement("div");
        errorNote.className = "meta";
        errorNote.textContent = "Lỗi khi tạo câu trả lời: " + streamError;
        wait.appendChild(errorNote);
      } else if (streamedFinal) {
        // The streamed final event already rendered the complete reply.
      } else if (typeof data.reply === "string") {
        const memoryNotes = Array.from(wait.querySelectorAll(".memory-result"));
        renderBotReply(wait, data.reply);
        if (data.model) {
          const meta = document.createElement("div");
          meta.className = "meta";
          meta.textContent = data.model;
          wait.appendChild(meta);
        }
        memoryNotes.forEach(note => wait.appendChild(note));
        restoreArtifactCards();
      }
    } else {
      if (!r.ok) throw new Error(data.error || "Yêu cầu thất bại");
      if (typeof data.reply === "string") {
        streamText.textContent = "";
        renderBotReply(wait, data.reply);
        if (data.model) {
          const meta = document.createElement("div");
          meta.className = "meta";
          meta.textContent = data.model;
          wait.appendChild(meta);
        }
        restoreArtifactCards();
      }
    }
    if (data.new) log.innerHTML = "";
    const reply = text === "/new" && data.new ? "" : data.reply || data.error || "(không có phản hồi)";
    const botMessage = reply ? wait : null;
    if (!reply) wait.remove();
    if (botMessage) {
      pollMemoryRecent(memorySince, botMessage);
      const memoryResult = data.memory_result;
      if (memoryResult && memoryResult.intent === "forget") {
        for (const item of memoryResult.deleted || []) {
          const note = document.createElement("div");
          note.className = "memory-result";
          note.textContent = "🧠 Đã xóa khỏi bộ nhớ: " + item.text;
          botMessage.appendChild(note);
        }
      }
      if (data.memory_error) {
        const note = document.createElement("div");
        note.className = "memory-result";
        note.textContent = "⚠ Lỗi bộ nhớ: " + data.memory_error;
        botMessage.appendChild(note);
      }
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
    if (streamText.textContent === "Đang trả lời...") {
      streamText.textContent = "";
    }
    const errorNote = document.createElement("div");
    errorNote.className = "meta";
    errorNote.textContent = "Lỗi kết nối tới server: " + e;
    wait.appendChild(errorNote);
  }
  sendBtn.disabled = false;
  box.focus();
  log.removeEventListener("scroll", trackScroll);
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
    if not any(entry["id"] == item["id"] for entry in list_memories()):
        return jsonify(error="Không xác minh được mục trong file bộ nhớ."), 500
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
    if item is None:
        return jsonify(error="Không tìm thấy mục ghi nhớ.", memories=list_memories()), 404
    result = delete_and_verify(memory_id)
    if not result["deleted"]:
        return jsonify(error=result["error"], memories=list_memories()), 500
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


@app.get("/artifact-view/<artifact_id>")
def artifact_view(artifact_id):
    with lock:
        project = current_project
    try:
        artifact = get_artifact(artifact_id, project)
    except FileNotFoundError:
        return Response("Không tìm thấy artifact trong project hiện tại.", status=404)
    except ValueError as error:
        return Response(str(error), status=400)

    title = html.escape(artifact["title"], quote=True)
    language = html.escape(artifact.get("language", "text"), quote=True)
    source = html.escape(artifact["content"], quote=False)
    version = artifact["versions"][-1]["number"]
    highlight_range = None
    try:
        highlight_start = int(request.args["highlight_start"])
        highlight_end = int(request.args["highlight_end"])
        highlight_version = int(request.args["highlight_version"])
        if (
            highlight_version == version
            and 0 <= highlight_start <= highlight_end <= len(artifact["content"])
        ):
            highlight_range = {
                "start": highlight_start,
                "end": highlight_end,
            }
    except (KeyError, TypeError, ValueError):
        pass
    if artifact["type"] == "markdown":
        blocks = [
            {
                "type": block["type"],
                "start": block["start"],
                "end": block["end"],
            }
            for block in split_markdown_blocks(artifact["content"])
        ]
        block_data = html.escape(
            json.dumps(blocks, ensure_ascii=False),
            quote=False,
        )
        content = '<main id="artifact-content" class="markdown-content"></main>'
        content_script = "renderMarkdownBlocks();"
    else:
        blocks = []
        offset = 0
        for line in artifact["content"].splitlines(keepends=True):
            end = offset + len(line)
            blocks.append({
                "type": "code_line",
                "start": offset,
                "end": end,
            })
            offset = end
        block_data = html.escape(
            json.dumps(blocks, ensure_ascii=False),
            quote=False,
        )
        content = (
            '<main id="artifact-content" class="code-content">'
            '<div class="language-label">' + language
            + '</div><pre><code id="code-source"></code></pre></main>'
        )
        content_script = "renderCodeLines();"
    page = (
        "<!DOCTYPE html><html lang=\"vi\"><head><meta charset=\"UTF-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>"
        + title
        + "</title><style>"
        "*{box-sizing:border-box}body{margin:0;padding:12px;height:100vh;"
        "display:flex;flex-direction:column;gap:10px;background:#1b1c1f;"
        "color:#e8e8ea;font-family:system-ui,sans-serif}.toolbar{display:flex;"
        "align-items:center;gap:8px;min-height:36px}.toolbar h1{font-size:1rem;"
        "font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"
        "flex:1;margin:0}.toolbar button{border:0;border-radius:6px;padding:6px 10px;"
        "background:#2e3036;color:#e8e8ea;cursor:pointer;white-space:nowrap}"
        ".toolbar button:hover{background:#3a3d45}.status{color:#8b8f99;"
        "font-size:.78rem;min-height:1em}#artifact-version{color:#aeb4c0;"
        "font-size:.8rem;white-space:nowrap}.markdown-content,.code-content{flex:1;"
        "min-height:0;overflow:auto;line-height:1.6;overflow-wrap:anywhere}"
        ".artifact-block{border-radius:4px}.artifact-block.selected-block{"
        "background:#354256;outline:1px solid #536984}"
        ".code-content{position:relative}.language-label{position:sticky;top:0;"
        "z-index:1;width:fit-content;margin-left:auto;padding:3px 8px;"
        "border-radius:0 0 0 6px;background:#24262b;color:#aeb4c0;font-size:.75rem}"
        "pre{margin:0;padding:14px;background:#111214;border-radius:8px;"
        "overflow:auto;white-space:pre;tab-size:4}code{font-family:Consolas,"
        "'Courier New',monospace;font-size:.9rem}.markdown-content code{"
        "background:#111214;padding:1px 5px;border-radius:4px}h3,h4,h5{"
        "font-weight:700;line-height:1.3;margin:8px 0 4px}h3{font-size:1.15rem}"
        "h4{font-size:1.08rem}h5{font-size:1rem}hr{border:0;border-top:1px solid "
        "#555;opacity:.6;margin:8px 0}a{color:#7cb2ff}"
        "#edit-form{padding:10px;border:1px solid #3a3d45;border-radius:8px;"
        "background:#24262b}#edit-form label{display:block;margin-bottom:6px;"
        "font-size:.9rem}#edit-instruction{width:100%;min-height:58px;resize:vertical;"
        "padding:8px;border:1px solid #3a3d45;border-radius:6px;background:#1b1c1f;"
        "color:#e8e8ea;font:inherit}#edit-actions{display:flex;gap:6px;"
        "margin-top:7px}#edit-actions button{padding:5px 10px}"
        "#edit-status{min-height:1.2em;margin-top:5px;color:#aeb4c0;font-size:.8rem}"
        "#edit-selection{position:fixed;z-index:5;padding:6px 9px;border:1px solid "
        "#536984;border-radius:7px;background:#2e3036;color:#e8e8ea;cursor:pointer;"
        "box-shadow:0 4px 14px #0008} [hidden]{display:none!important}"
        "</style></head><body><header class=\"toolbar\"><h1>"
        + title
        + '</h1><span id="artifact-version">v'
        + str(version)
        + '</span><button id="copy" type="button">Sao chép</button>'
        '<button id="download" type="button">Tải về</button></header>'
        '<div id="status" class="status" aria-live="polite"></div>'
        '<form id="edit-form" hidden><label for="edit-instruction">'
        "Bạn muốn sửa thế nào?</label>"
        '<textarea id="edit-instruction" rows="2"></textarea>'
        '<div id="edit-actions"><button id="edit-send" type="submit">Gửi</button>'
        '<button id="edit-cancel" class="ghost" type="button">Hủy</button></div>'
        '<div id="edit-status" aria-live="polite"></div></form>'
        '<button id="edit-selection" type="button" hidden>✏️ Sửa đoạn này</button>'
        '<textarea id="artifact-source" hidden>'
        + source
        + "</textarea>"
        '<textarea id="artifact-block-data" hidden>'
        + block_data
        + "</textarea>"
        + content
        + "<script>"
        "function render(t){"
        'const esc=s=>s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");'
        'const inline=line=>line.replace(/`([^`]+)`/g,"<code>$1</code>")'
        '.replace(/\\*\\*([^*]+)\\*\\*/g,"<b>$1</b>")'
        '.replace(/(^|[\\s:])\\/bilingual\\b/g,\'$1<a href="/bilingual">/bilingual</a>\')'
        '.replace(/(^|[\\s:])\\/diagram\\b/g,\'$1<a href="/diagram">/diagram</a>\')'
        '.replace(/(^|[\\s:])\\/pdfview\\/(\\d+)\\b/g,\'$1<a href="/pdfview/$2">/pdfview/$2</a>\')'
        '.replace(/(^|[\\s:])\\/(vis-play|vis-view)\\b/g,\'$1<a href="/$2">/$2</a>\')'
        '.replace(/(^|[\\s:])\\/download\\/([^\\s<"\']+)/g,\'$1<a href="/download/$2">/download/$2</a>\')'
        '.replace(/(^|[\\s:])\\/files\\/([^\\s<"\']+)/g,\'$1<a href="/files/$2">/files/$2</a>\')'
        '.replace(/(https?:\\/\\/[^\\s<"\']+)/g,\'<a href="$1" target="_blank" rel="noopener">$1</a>\');'
        'return esc(t).split("```").map((p,i)=>i%2?'
        '"<pre><code>"+p.replace(/^[a-zA-Z0-9]*\\n/,"")+"</code></pre>":'
        'p.split("\\n").map(line=>{const heading=line.match(/^(#{1,3}) (.+)$/);'
        'if(heading){const level=2+heading[1].length;'
        'return `<h${level}>${inline(heading[2])}</h${level}>`;}'
        'if(line==="---")return "<hr>";return inline(line);}).join("<br>")).join("");}'
        "const sourceElement=document.getElementById('artifact-source');"
        "let source=sourceElement.value;"
        "const artifactBlocks=JSON.parse(document.getElementById('artifact-block-data').value);"
        "const artifactId="
        + json.dumps(artifact_id)
        + ";let currentVersion="
        + str(version)
        + ";const highlightRange="
        + json.dumps(highlight_range)
        + ";const codeUnitOffsets=[0];"
        "for(let index=0;index<source.length;){"
        "const point=source.codePointAt(index);index+=point>65535?2:1;"
        "codeUnitOffsets.push(index);}"
        "function sourceSlice(start,end){"
        "return source.slice(codeUnitOffsets[start],codeUnitOffsets[end]);}"
        "function renderMarkdownBlocks(){"
        "const root=document.getElementById('artifact-content');root.replaceChildren();"
        "artifactBlocks.forEach(block=>{const element=document.createElement('div');"
        "element.className='artifact-block';element.dataset.start=block.start;"
        "element.dataset.end=block.end;"
        "element.innerHTML=render(sourceSlice(block.start,block.end));"
        "root.appendChild(element);});}"
        "function renderCodeLines(){"
        "const code=document.getElementById('code-source');code.replaceChildren();"
        "artifactBlocks.forEach(block=>{const line=document.createElement('span');"
        "line.className='artifact-block code-line';line.dataset.start=block.start;"
        "line.dataset.end=block.end;line.textContent=sourceSlice(block.start,block.end);"
        "code.appendChild(line);});}"
        "const contentRoot=document.getElementById('artifact-content');"
        "const editButton=document.getElementById('edit-selection');"
        "const editForm=document.getElementById('edit-form');"
        "const editInput=document.getElementById('edit-instruction');"
        "const editStatus=document.getElementById('edit-status');"
        "let activeSelection=null;let activeBlocks=[];let patchInProgress=false;"
        "function clearBlockHighlight(){"
        "activeBlocks.forEach(block=>block.classList.remove('selected-block'));"
        "activeBlocks=[];}"
        "function positionEditButton(){"
        "if(!editForm.hidden){editButton.hidden=true;return;}"
        "const selection=window.getSelection();"
        "if(!selection||selection.isCollapsed||!selection.rangeCount||"
        "!contentRoot.contains(selection.anchorNode)||"
        "!contentRoot.contains(selection.focusNode)){"
        "editButton.hidden=true;activeSelection=null;return;}"
        "const range=selection.getRangeAt(0);"
        "const blocks=Array.from(contentRoot.querySelectorAll('.artifact-block'))"
        ".filter(block=>{try{return range.intersectsNode(block);}catch(_error){return false;}});"
        "if(!blocks.length){editButton.hidden=true;activeSelection=null;return;}"
        "activeBlocks=blocks;"
        "activeSelection={"
        "start:Math.min(...blocks.map(block=>Number(block.dataset.start))),"
        "end:Math.max(...blocks.map(block=>Number(block.dataset.end)))};"
        "editButton.hidden=false;const rect=range.getBoundingClientRect();"
        "const left=Math.max(8,Math.min(window.innerWidth-editButton.offsetWidth-8,rect.left));"
        "editButton.style.left=left+'px';"
        "editButton.style.top=Math.max(8,rect.top-40)+'px';}"
        "document.addEventListener('selectionchange',positionEditButton);"
        "document.addEventListener('mouseup',positionEditButton);"
        "document.addEventListener('keyup',positionEditButton);"
        "editButton.addEventListener('mousedown',event=>event.preventDefault());"
        "editButton.addEventListener('click',()=>{"
        "if(!activeSelection)return;"
        "activeBlocks.forEach(block=>block.classList.add('selected-block'));"
        "editButton.hidden=true;editForm.hidden=false;editInput.value='';"
        "editStatus.textContent='';editInput.focus();});"
        "function closeEditForm(){"
        "if(patchInProgress)return;clearBlockHighlight();activeSelection=null;"
        "editForm.hidden=true;editInput.value='';editStatus.textContent='';"
        "const selection=window.getSelection();if(selection)selection.removeAllRanges();}"
        "document.getElementById('edit-cancel').addEventListener('click',closeEditForm);"
        "editForm.addEventListener('submit',async event=>{"
        "event.preventDefault();if(!activeSelection||patchInProgress)return;"
        "const instruction=editInput.value.trim();"
        "if(!instruction){editStatus.textContent='Hãy nhập yêu cầu chỉnh sửa.';return;}"
        "patchInProgress=true;editInput.disabled=true;"
        "document.getElementById('edit-send').disabled=true;"
        "document.getElementById('edit-cancel').disabled=true;"
        "editStatus.textContent='Đang sửa…';"
        "try{const response=await fetch('/api/artifacts/'+encodeURIComponent(artifactId)"
        "+'/patch',{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({base_version:currentVersion,start:activeSelection.start,"
        "end:activeSelection.end,instruction})});"
        "const data=await response.json().catch(()=>({}));"
        "if(!response.ok)throw new Error(data.error||'Không sửa được đoạn Artifact.');"
        "if(typeof data.content!=='string'||!Number.isInteger(data.version)||"
        "!Number.isInteger(data.start)||!Number.isInteger(data.end_moi)){"
        "throw new Error('Phản hồi sửa Artifact không hợp lệ.');}"
        "const query=new URLSearchParams({highlight_start:String(data.start),"
        "highlight_end:String(data.end_moi),highlight_version:String(data.version)});"
        "window.location.replace(location.pathname+'?'+query.toString());"
        "}catch(error){"
        "editStatus.textContent=error.message||'Không sửa được đoạn Artifact.';"
        "patchInProgress=false;editInput.disabled=false;"
        "document.getElementById('edit-send').disabled=false;"
        "document.getElementById('edit-cancel').disabled=false;}});"
        "function highlightPatchedBlocks(){"
        "if(!highlightRange)return;let first=null;"
        "contentRoot.querySelectorAll('.artifact-block').forEach(block=>{"
        "const start=Number(block.dataset.start),end=Number(block.dataset.end);"
        "if(start<highlightRange.end&&end>highlightRange.start){"
        "block.classList.add('selected-block');if(!first)first=block;}});"
        "if(first){first.scrollIntoView({block:'nearest'});"
        "setTimeout(()=>contentRoot.querySelectorAll('.selected-block')"
        ".forEach(block=>block.classList.remove('selected-block')),3000);}}"
        + content_script
        + "highlightPatchedBlocks();"
        "const status=document.getElementById('status');"
        "document.getElementById('copy').addEventListener('click',async()=>{"
        "try{await navigator.clipboard.writeText(source);status.textContent='Đã sao chép.';}"
        "catch(error){status.textContent='Không thể sao chép: '+error.message;}});"
        "document.getElementById('download').addEventListener('click',()=>{"
        "const type="
        + json.dumps(artifact["type"])
        + ";const language="
        + json.dumps(artifact.get("language", "")).replace("<", "\\u003c")
        + ";const title="
        + json.dumps(artifact["title"]).replace("<", "\\u003c")
        + ";const extensions={python:'py',py:'py',javascript:'js',js:'js',"
        "typescript:'ts',ts:'ts',jsx:'jsx',tsx:'tsx',java:'java',c:'c',"
        "cpp:'cpp','c++':'cpp',csharp:'cs','c#':'cs',go:'go',rust:'rs',"
        "ruby:'rb',php:'php',html:'html',css:'css',json:'json',sql:'sql',"
        "bash:'sh',shell:'sh',sh:'sh',powershell:'ps1',yaml:'yml',"
        "kotlin:'kt',swift:'swift',r:'r',markdown:'md'};"
        "const normalized=language.trim().toLowerCase();"
        "const extension=type==='markdown'?'md':(extensions[normalized]||"
        "normalized.replace(/[^a-z0-9]+/g,'')||'txt');"
        "const base=title.replace(/[\\\\/:*?\"<>|]+/g,'_').trim()||'artifact';"
        "const url=URL.createObjectURL(new Blob([source],{type:'text/plain;charset=utf-8'}));"
        "const link=document.createElement('a');link.href=url;"
        "link.download=base+'.'+extension;link.click();"
        "setTimeout(()=>URL.revokeObjectURL(url),1000);});"
        "</script></body></html>"
    )
    return Response(page, mimetype="text/html")


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


@app.get("/api/artifacts")
def api_artifacts():
    with lock:
        project = current_project
    try:
        return jsonify(artifacts=list_artifacts(project))
    except ValueError as error:
        return jsonify(error=str(error)), 400


@app.get("/api/artifacts/<artifact_id>")
def api_artifact(artifact_id):
    with lock:
        project = current_project
    try:
        artifact = get_artifact(artifact_id, project)
    except FileNotFoundError:
        return jsonify(error="Không tìm thấy artifact trong project hiện tại."), 404
    except ValueError as error:
        return jsonify(error=str(error)), 400
    return jsonify(artifact=artifact)


def _explicit_structure_change_requested(instruction):
    action = (
        r"(?:thêm|bổ sung|xóa|xoá|bỏ|bớt|loại bỏ|add|insert|delete|remove|"
        r"omit|drop)"
    )
    target = (
        r"(?:tiêu đề|mục|dòng|khối|đoạn|danh sách|bullet|heading|item|line|"
        r"block|section|paragraph)"
    )
    return re.search(
        rf"\b{action}\b.{{0,40}}\b{target}\b"
        rf"|\b{target}\b.{{0,40}}\b{action}\b",
        instruction,
        re.IGNORECASE,
    ) is not None


@app.post("/api/artifacts/<artifact_id>/patch")
def api_artifact_patch(artifact_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Yêu cầu phải là JSON object."), 400
    base_version = data.get("base_version")
    start = data.get("start")
    end = data.get("end")
    instruction = data.get("instruction")
    if (
        type(base_version) is not int
        or type(start) is not int
        or type(end) is not int
        or not isinstance(instruction, str)
        or not instruction.strip()
    ):
        return jsonify(
            error="Cần base_version, start, end hợp lệ và instruction không rỗng."
        ), 400

    with lock:
        project = current_project
    try:
        artifact = get_artifact(artifact_id, project)
    except FileNotFoundError:
        return jsonify(error="Không tìm thấy artifact trong project hiện tại."), 404
    except ValueError as error:
        return jsonify(error=str(error)), 400

    latest_version = artifact["versions"][-1]["number"]
    if base_version != latest_version:
        return jsonify(
            error=(
                f"Artifact đã đổi sang version {latest_version}; "
                f"base_version {base_version} đã cũ. Hãy tải lại artifact."
            )
        ), 409
    content = artifact["content"]
    if start < 0 or end < start or end > len(content):
        return jsonify(
            error=(
                f"Vùng chọn [{start}, {end}) nằm ngoài nội dung "
                f"dài {len(content)} ký tự."
            )
        ), 400

    context_start = max(0, start - 300)
    context_end = min(len(content), end + 300)
    selected_text = content[start:end]
    context_before = content[context_start:start]
    context_after = content[end:context_end]
    artifact_format = (
        f"code, ngôn ngữ {artifact.get('language', 'không xác định')}"
        if artifact["type"] == "code"
        else "Markdown"
    )
    prompt = (
        f"Chỉnh sửa Artifact dạng {artifact_format} theo yêu cầu bên dưới. "
        "Chỉ trả về nội dung thay thế cho đoạn được chọn, không giải thích. "
        "Giữ nguyên cấu trúc Markdown của đoạn gốc: cùng số lượng và loại "
        "tiêu đề, mục danh sách, khối code; giữ nguyên cách xuống dòng, mỗi "
        "mục danh sách một dòng riêng. Chỉ đổi nội dung chữ theo yêu cầu, "
        "không gộp các dòng hoặc khối lại. "
        "Nếu đây là code, chỉ trả về code thay thế đúng đoạn đã chọn, "
        "không bọc thêm ```.\n\n"
        f"Ngữ cảnh trước:\n{context_before}\n\n"
        f"ĐOẠN ĐƯỢC CHỌN:\n{selected_text}\n\n"
        f"Ngữ cảnh sau:\n{context_after}\n\n"
        f"Yêu cầu: {instruction.strip()}"
    )
    try:
        replacement, _model, _meta = ask(
            gemini_client,
            openai_clients,
            [],
            prompt,
            use_history=False,
            route_text=instruction,
            include_persistent_context=False,
            allow_tools=False,
            project=project,
        )
    except Exception as error:
        return jsonify(error=f"Không thể sửa đoạn Artifact: {error}"), 502
    if not isinstance(replacement, str) or not replacement.strip():
        return jsonify(
            error="Model trả nội dung thay thế rỗng; Artifact chưa được thay đổi."
        ), 502

    selected_leading_length = len(selected_text) - len(selected_text.lstrip())
    selected_trailing_start = len(selected_text.rstrip())
    if selected_trailing_start < selected_leading_length:
        selected_trailing_start = selected_leading_length
    selected_leading = selected_text[:selected_leading_length]
    selected_trailing = selected_text[selected_trailing_start:]

    replacement = replacement.strip()
    original_is_fenced = re.fullmatch(
        r"```[^\r\n]*\r?\n.*?\r?\n```",
        selected_text.strip(),
        re.DOTALL,
    ) is not None
    fenced_reply = re.fullmatch(
        r"```[^\r\n]*\r?\n(.*?)\r?\n```",
        replacement,
        re.DOTALL,
    )
    if fenced_reply:
        unwrapped_reply = fenced_reply.group(1).strip()
        wrapped_inner_fence = re.fullmatch(
            r"```[^\r\n]*\r?\n.*?\r?\n```",
            unwrapped_reply,
            re.DOTALL,
        ) is not None
        if not original_is_fenced or wrapped_inner_fence:
            replacement = unwrapped_reply

    original_blocks = split_markdown_blocks(selected_text)
    replacement_blocks = split_markdown_blocks(replacement)
    original_structure = [block["type"] for block in original_blocks]
    replacement_structure = [block["type"] for block in replacement_blocks]
    if (
        original_structure != replacement_structure
        and not _explicit_structure_change_requested(instruction)
    ):
        return jsonify(
            error=(
                "Kết quả sửa làm đổi cấu trúc, thử lại hoặc chọn đoạn nhỏ hơn."
            )
        ), 422

    replacement = selected_leading + replacement + selected_trailing
    try:
        updated = apply_artifact_patch(
            artifact_id,
            base_version,
            start,
            end,
            replacement,
            "bot",
            project=project,
        )
    except ArtifactVersionConflict as error:
        return jsonify(error=str(error)), 409
    except FileNotFoundError:
        return jsonify(error="Không tìm thấy artifact trong project hiện tại."), 404
    except ValueError as error:
        return jsonify(error=str(error)), 400
    return jsonify(
        version=updated["versions"][-1]["number"],
        content=updated["content"],
        start=start,
        end_moi=start + len(replacement),
    )


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
    global current_chat_id, current_extracted_upto
    global current_summary, current_summary_upto, messages
    path = _chat_path(chat_id)
    if path is None:
        return jsonify(error="Không tìm thấy cuộc chat."), 404
    with lock:
        if path.is_symlink() or not path.is_file():
            return jsonify(error="Không tìm thấy cuộc chat."), 404
        _save_current_chat()
        chat_data = _read_chat(path)
        messages = chat_data["messages"]
        current_chat_id = chat_id
        current_extracted_upto = chat_data.get("extracted_upto", 0)
        current_summary = chat_data.get("summary", "")
        current_summary_upto = chat_data.get("summary_upto", 0)
        save_history(
            messages,
            summary=current_summary,
            summary_upto=current_summary_upto,
        )
        opened_messages = messages[:]
    return jsonify(messages=opened_messages, current_id=chat_id)


@app.post("/chats/delete/<chat_id>")
def delete_chat(chat_id):
    global current_chat_id, current_extracted_upto
    global current_summary, current_summary_upto, messages
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
            current_summary = ""
            current_summary_upto = 0
            messages = []
            save_history(messages, summary="", summary_upto=0)
    return jsonify(ok=True, current_deleted=deleted_current)


def _process_chat_message(text, event_sink=None, artifact_refs=None):
    global current_chat_id, current_extracted_upto
    global current_summary, current_summary_upto, messages
    if text == "/new":
        _save_current_chat()
        messages = []
        current_chat_id = None
        current_extracted_upto = 0
        current_summary = ""
        current_summary_upto = 0
        save_history(messages, summary="", summary_upto=0)
        return {"reply": "Đã bắt đầu cuộc chat mới.", "model": "", "new": True}
    if text == "/nho" or text.startswith("/nho "):
        memory_text = text[4:].strip()
        if not memory_text:
            return {"reply": "Dùng: /nho nội dung cần ghi nhớ", "model": ""}
        try:
            save_result = save_and_verify(memory_text)
        except ValueError as error:
            return {"reply": str(error), "model": ""}
        if save_result["saved"]:
            reply = "Đã ghi nhớ thông tin trên mọi project."
            _record_memory_event("remembered", memory_text)
        else:
            reply = f"Chưa lưu được thông tin: {save_result['error']}"
        result = {"reply": reply, "model": ""}
        if save_result["saved"]:
            result["memory_result"] = {
                "intent": "remember",
                "saved": True,
                "text": memory_text,
            }
        else:
            result["memory_error"] = save_result["error"]
        return result
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
        memories = list_memories()
        number = int(match.group(1))
        if number < 1 or number > len(memories):
            return {"reply": "Không tìm thấy mục bộ nhớ theo số đó.", "model": ""}
        deleted = memories[number - 1]
        result = delete_and_verify(deleted["id"])
        if not result["deleted"]:
            return {"reply": f"Không xóa được mục bộ nhớ: {result['error']}", "model": ""}
        _record_memory_event("forgot", deleted["text"])
        return {
            "reply": "Đã xóa mục khỏi bộ nhớ.",
            "model": "",
            "memory_result": {
                "intent": "forget",
                "deleted": [deleted],
                "error": None,
            },
        }
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
        current_summary = ""
        current_summary_upto = 0
        save_history(messages, summary="", summary_upto=0)
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
        record_usage(model_key, meta)
        return {
            "reply": reply,
            "model": model_key,
            "usage": format_usage(load_usage(), model_key),
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
        record_usage(model_key, meta)
        return {
            "reply": f"{reply}\n{note}",
            "model": model_key,
            "usage": format_usage(load_usage(), model_key),
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
    stream_error = None
    if event_sink is None:
        try:
            answer, model_key, meta, memory_result = run_chat_turn(
                gemini_client,
                openai_clients,
                messages,
                prompt,
                question,
                ask_fn=ask,
                record_usage_fn=record_usage,
                use_history=not sources,
                project=current_project,
                summary=current_summary,
                summary_upto=current_summary_upto,
            )
        except Exception as e:
            return {"reply": f"Lỗi: tất cả model đều thất bại. {e}", "model": ""}
    else:
        final_event = None
        streamed_text = []
        try:
            for event in run_chat_turn_stream(
                gemini_client,
                openai_clients,
                messages,
                prompt,
                question,
                ask_stream_fn=ask_stream,
                ask_fn=ask,
                record_usage_fn=record_usage,
                use_history=not sources,
                project=current_project,
                summary=current_summary,
                summary_upto=current_summary_upto,
            ):
                if event["type"] == "token":
                    streamed_text.append(event.get("text", ""))
                    event_sink(event)
                elif event["type"] == "final":
                    final_event = event
                elif event["type"] == "error":
                    stream_error = event
                else:
                    event_sink(event)
        except Exception as error:
            stream_error = {
                "type": "error",
                "message": str(error),
                "partial": "".join(streamed_text),
            }

        if stream_error:
            answer = stream_error.get("partial") or "".join(streamed_text)
            model_key = stream_error.get("model", "")
            meta = None
            memory_result = stream_error.get("memory_result")
            if not answer:
                result = {
                    "reply": (
                        "Lỗi: tất cả model đều thất bại. "
                        f"{stream_error['message']}"
                    ),
                    "model": "",
                }
                if memory_result:
                    result["memory_result"] = memory_result
                result["_stream_error"] = stream_error["message"]
                return result
        elif final_event is not None:
            answer = final_event["reply"]
            model_key = final_event["model"]
            meta = final_event.get("_meta")
            memory_result = final_event.get("memory_result")
        else:
            message = "Luồng model kết thúc mà không có sự kiện final."
            return {
                "reply": f"Lỗi: {message}",
                "model": "",
                "_stream_error": message,
            }

    saved = answer
    if artifact_refs:
        saved = "\n".join(
            f"Artifact: {artifact['title']} (id: {artifact['id']})"
            for artifact in artifact_refs
        )
    if stream_error and saved:
        saved += f"\n\n[Lỗi khi tiếp tục: {stream_error['message']}]"
    if sources and not artifact_refs:
        saved += "\n\nNguồn:\n" + "\n".join(
            f"[{i}] {t}: {u}" for i, (t, u) in enumerate(sources, 1))
    messages.append({"role": "user", "text": question})
    messages.append({"role": "model", "text": saved})
    save_history(messages)
    if event_sink is None or (model_key and meta is not None):
        record_usage(model_key, meta)
    result = {
        "reply": saved,
        "model": model_key,
    }
    if model_key:
        result["usage"] = format_usage(load_usage(), model_key)
    if stream_error:
        result["_stream_error"] = stream_error["message"]
    if memory_result:
        result["memory_result"] = memory_result
        if memory_result["intent"] == "remember":
            if not memory_result["saved"] and memory_result["error"]:
                result["memory_error"] = memory_result["error"]
        else:
            if memory_result["error"]:
                result["memory_error"] = memory_result["error"]
    if looks_personal(text):
        result["suggest_pin"] = text
    return result


def _run_chat_job(job_id, text):
    global active_job_id
    confirm_context.job_id = job_id
    pending_errors = []
    artifact_refs = []

    def publish_event(event):
        if event["type"] == "error":
            pending_errors.append(event)
        else:
            _publish_chat_event(job_id, event)

    def publish_artifact(artifact):
        artifact_refs.append(artifact)
        _publish_chat_event(job_id, {
            "type": "artifact",
            "id": artifact["id"],
            "title": artifact["title"],
            "artifact_type": artifact["artifact_type"],
        })

    previous_artifact_handler = set_artifact_handler(publish_artifact)
    try:
        with lock:
            messages_before = messages[:]
            workspace_before = _workspace_file_mtimes()
            try:
                result = _process_chat_message(
                    text,
                    event_sink=publish_event,
                    artifact_refs=artifact_refs,
                )
            except Exception as e:
                result = {"reply": f"Lỗi xử lý: {e}", "model": ""}
                pending_errors.append({"type": "error", "message": str(e)})
            stream_error = result.pop("_stream_error", None)
            if stream_error:
                pending_errors.append({
                    "type": "error",
                    "message": stream_error,
                })
            original_reply = result.get("reply")
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
            if (
                result.get("reply") != original_reply
                and len(messages) == len(messages_before) + 2
                and messages[:-2] == messages_before
                and messages[-2].get("role") == "user"
                and messages[-1].get("role") == "model"
                and messages[-1].get("text") == original_reply
            ):
                messages[-1]["text"] = result["reply"]
                save_history(
                    messages,
                    summary=current_summary,
                    summary_upto=current_summary_upto,
                )
            _save_current_chat()
            memory_result = result.get("memory_result")
            if (
                memory_result
                and memory_result.get("intent") == "remember"
                and memory_result.get("saved")
            ):
                with chat_jobs_lock:
                    job = chat_jobs.get(job_id)
                    memory_event_sent = bool(
                        job and job.get("memory_event_sent")
                    )
                if not memory_event_sent:
                    _publish_chat_event(job_id, {
                        "type": "memory",
                        "text": memory_result.get("text", ""),
                    })
            if (
                not result.get("new")
                and len(messages) == len(messages_before) + 2
                and messages[:-2] == messages_before
                and messages[-2].get("role") == "user"
                and messages[-1].get("role") == "model"
            ):
                project_snapshot = current_project
                chat_id_snapshot = current_chat_id
                schedule_memory_scan(
                    gemini_client,
                    openai_clients,
                    messages,
                    ask_fn=ask,
                    record_usage_fn=record_usage,
                    on_saved=lambda item: _record_memory_event("scanned", item),
                )

                def persist_summary(summary, summary_upto):
                    _save_chat_summary(
                        project_snapshot,
                        chat_id_snapshot,
                        summary,
                        summary_upto,
                    )

                schedule_thread_summary(
                    gemini_client,
                    openai_clients,
                    messages,
                    current_summary,
                    current_summary_upto,
                    ask_fn=ask,
                    record_usage_fn=record_usage,
                    persist_fn=persist_summary,
                    chat_key=(project_snapshot, chat_id_snapshot),
                )
    except Exception as e:
        result = {"reply": f"Lỗi xử lý: {e}", "model": ""}
        pending_errors.append({"type": "error", "message": str(e)})
    finally:
        set_artifact_handler(previous_artifact_handler)
        del confirm_context.job_id
        with chat_jobs_condition:
            job = chat_jobs.get(job_id)
            if job is not None:
                job["result"] = result
                job["status"] = "done"
                job["confirmation"] = None
                if pending_errors:
                    error_event = pending_errors[-1]
                    _append_chat_event_locked(job, {
                        "type": "error",
                        "message": error_event["message"],
                    })
                else:
                    if not job["final_event_sent"]:
                        _append_chat_event_locked(job, {
                            "type": "final",
                            "reply": result.get("reply", ""),
                            "model": result.get("model", ""),
                        })
                    _append_chat_event_locked(job, {"type": "done"})
            if active_job_id == job_id:
                active_job_id = None
            chat_jobs_condition.notify_all()


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
        _prune_chat_jobs_locked(now)
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
            "events": [],
            "last_event_id": 0,
            "memory_event_sent": False,
            "final_event_sent": False,
        }
        active_job_id = job_id
        threading.Thread(
            target=_run_chat_job, args=(job_id, text), daemon=True).start()
    response = {"pending": True, "request_id": job_id}
    if looks_personal(text):
        response["suggest_pin"] = text
    return jsonify(response), 202


@app.get("/events/<request_id>")
def chat_events(request_id):
    raw_last_event_id = request.headers.get("Last-Event-ID", "0")
    try:
        cursor = int(raw_last_event_id)
    except ValueError:
        return jsonify(error="Last-Event-ID phải là số nguyên."), 400
    if cursor < 0:
        return jsonify(error="Last-Event-ID không được âm."), 400
    with chat_jobs_condition:
        _prune_chat_jobs_locked(time.monotonic())
        if request_id not in chat_jobs:
            return jsonify(error="Không tìm thấy yêu cầu chat."), 404

    @stream_with_context
    def generate():
        nonlocal cursor
        while True:
            with chat_jobs_condition:
                job = chat_jobs.get(request_id)
                if job is None:
                    return
                pending = [
                    dict(event)
                    for event in job["events"]
                    if event["id"] > cursor
                ]
                if not pending:
                    if job["status"] == "done":
                        return
                    notified = chat_jobs_condition.wait(timeout=15)
                    heartbeat = not notified
                else:
                    heartbeat = False
            if heartbeat:
                yield ": keep-alive\n\n"
                continue
            if not pending:
                continue
            for event in pending:
                if event["id"] <= cursor:
                    continue
                cursor = event["id"]
                payload = json.dumps(
                    event["data"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                event_type = event["data"]["type"]
                yield (
                    f"id: {cursor}\nevent: {event_type}\n"
                    f"data: {payload}\n\n"
                )
                if event_type in ("done", "error"):
                    return

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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