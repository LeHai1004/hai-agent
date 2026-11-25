import os
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import tools
from pins import add_pin, format_pins, load_pins, remove_pin
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from google import genai
from google.genai import types
from openai import OpenAI
from werkzeug.utils import secure_filename

from history import load_history, save_history
from llm import ask
from filechat import ask_file
from img import ask_image
from pdfread import read_pdf_page
from search import build_prompt, web_search
from translate import translate
from usage import format_usage, load_usage, record_usage

load_dotenv()
app = Flask(__name__)
MAX_UPLOAD_SIZE = 30 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".txt", ".md", ".py",
    ".java", ".kt", ".cs", ".json", ".xml", ".html", ".css", ".js", ".sql",
}
IMAGE_UPLOAD_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE + 64 * 1024
lock = threading.Lock()
chat_jobs_lock = threading.Lock()
chat_jobs = {}
active_job_id = None
confirm_context = threading.local()


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


gemini_client, openai_clients = make_clients()
messages = load_history()
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
        approved = (
            signaled
            and job["decision"] is True
            and time.monotonic() < deadline
        )
        job["status"] = "running"
        job["confirmation"] = None
        job["decision"] = None
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
button{border:0;border-radius:8px;padding:9px 16px;background:#3b82f6;color:#fff;cursor:pointer;font-size:.95rem}
button:disabled{background:#4b5060;cursor:not-allowed}
.ghost{background:#2e3036}
#log{flex:1;overflow-y:auto;padding:20px;max-width:860px;width:100%;margin:0 auto}
#layout{display:flex;flex:1;min-height:0;width:100%}
#chat{display:flex;flex:1 1 100%;flex-direction:column;min-width:0}
body.panel-open #chat{flex:0 0 45%}
#panel{display:none;flex:0 0 55%;flex-direction:column;min-width:0;min-height:0;border-left:1px solid #2e3036;background:#202125}
body.panel-open #panel{display:flex}
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
pre{background:#111214;padding:12px;border-radius:8px;overflow-x:auto}
code{background:#111214;padding:1px 5px;border-radius:4px;font-family:Consolas,monospace}
pre code{padding:0}
a{color:#7cb2ff}
footer{border-top:1px solid #2e3036;padding:14px 20px}
.row{display:flex;gap:10px;max-width:860px;margin:0 auto}
textarea{flex:1;resize:none;height:56px;padding:12px;border-radius:10px;border:1px solid #3a3d45;background:#24262b;color:#e8e8ea;font-size:1rem;font-family:inherit}
.hint{max-width:860px;margin:6px auto 0;color:#8b8f99;font-size:.78rem}
</style></head><body>
<header><b>Hai Agent</b><button class="ghost" id="new">Chat mới</button></header>
<main id="layout">
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
<aside id="panel">
<div class="panel-head"><b id="panel-title"></b><button class="ghost" id="close-panel" aria-label="Đóng panel">✕</button></div>
<iframe id="panel-frame" title="Nội dung"></iframe>
</aside>
</main>
<script>
const log = document.getElementById("log");
const box = document.getElementById("box");
const sendBtn = document.getElementById("send");
const panelFrame = document.getElementById("panel-frame");
const panelTitle = document.getElementById("panel-title");
const attachBtn = document.getElementById("attach");
const pdfFileInput = document.getElementById("pdf-file");
let pendingImagePreview = null;
function openPanel(url, title){
  panelTitle.textContent = title;
  panelFrame.src = url + (url.includes("?") ? "&" : "?") + "t=" + Date.now();
  document.body.classList.add("panel-open");
}
document.getElementById("close-panel").addEventListener("click", () => {
  document.body.classList.remove("panel-open");
});
const panelTitles = {"/bilingual":"Dịch song ngữ","/read":"Đọc tài liệu","/vis":"Trực quan hóa"};
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
  return esc(t).split("```").map((p,i) => i % 2
    ? "<pre><code>" + p.replace(/^[a-zA-Z0-9]*\\n/,"") + "</code></pre>"
    : p.replace(/`([^`]+)`/g,"<code>$1</code>")
       .replace(/\\*\\*([^*]+)\\*\\*/g,"<b>$1</b>")
       .replace(/(^|[\\s:])\\/bilingual\\b/g,'$1<a href="/bilingual">/bilingual</a>')
       .replace(/(^|[\\s:])\\/pdfview\\/(\\d+)\\b/g,'$1<a href="/pdfview/$2">/pdfview/$2</a>')
       .replace(/(https?:\\/\\/[^\\s<"']+)/g,'<a href="$1" target="_blank" rel="noopener">$1</a>')
       .replace(/\\n/g,"<br>")).join("");
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
function addConfirmation(requestId, confirmation){
  const card = document.createElement("div");
  card.className = "msg bot confirmation";
  const prompt = document.createElement("div");
  prompt.textContent = `⚠ Tool '${confirmation.tool}' ${confirmation.reason}.\nGợi ý: ${confirmation.suggestion}.\n\nVẫn chạy?`;
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
async function send(){
  const text = box.value.trim();
  if (!text) return;
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
    const reply = data.reply || data.error || "(không có phản hồi)";
    addMsg("bot", reply, data.model || "");
    if (reply.includes("/bilingual")) openPanel("/bilingual", "Dịch song ngữ");
    const pdfMatch = reply.match(/\\/pdfview\\/(\\d+)\\b/);
    if (pdfMatch) openPanel(`/pdfview/${pdfMatch[1]}`, `PDF trang ${pdfMatch[1]}`);
    if (data.usage) document.getElementById("usage").textContent = data.usage;
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
fetch("/api/history").then(r => r.json()).then(list => {
  list.forEach(m => addMsg(m.role === "user" ? "user" : "bot", m.text));
});
box.focus();
</script></body></html>"""


@app.get("/")
def index():
    return PAGE


@app.get("/bilingual")
def bilingual():
    return send_from_directory("workspace", "bilingual.html")


@app.get("/pdfview/<int:page_no>")
def pdf_view(page_no):
    return send_from_directory("workspace", f"pdf_trang{page_no}.html")


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
    os.makedirs("workspace", exist_ok=True)
    Path("workspace", filename).write_bytes(content)
    kind = "pdf" if extension == ".pdf" else (
        "image" if extension in IMAGE_UPLOAD_EXTENSIONS else "text")
    return jsonify(path=f"workspace/{filename}", kind=kind)


@app.get("/workspace-img/<path:filename>")
def workspace_image(filename):
    if ".." in filename or "/" in filename or "\\" in filename:
        return "", 404
    if os.path.splitext(filename)[1].lower() not in IMAGE_UPLOAD_EXTENSIONS:
        return "", 404
    return send_from_directory("workspace", filename)


@app.get("/api/history")
def history():
    return jsonify(messages)


def _process_chat_message(text):
    global messages
    if text == "/new":
        messages = []
        save_history(messages)
        return {"reply": "Đã bắt đầu cuộc chat mới.", "model": "", "new": True}
    if text == "/pins" or text.startswith("/pin ") or text.startswith("/unpin"):
        pins = load_pins()
        if text == "/pins":
            reply = format_pins(pins)
        elif text.startswith("/unpin"):
            reply = remove_pin(pins, text[6:].strip())
        else:
            reply = add_pin(pins, text[5:])
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
            "(Đã mở workspace/bilingual.html)",
            "Đã dịch xong. Mở bản song ngữ: /bilingual",
        )
        return {"reply": reply, "model": model_key}
    if text == "/read" or text.startswith("/read "):
        args = text[5:].strip()
        page_no = 1
        head, _, tail = args.rpartition(" ")
        if head and tail.isdigit():
            args, page_no = head.strip(), int(tail)
        args = args.strip('"')
        if not args:
            return {"reply": "Dùng: /read đường_dẫn.pdf [số trang]", "model": ""}
        try:
            reply, model_key = read_pdf_page(
                gemini_client, openai_clients, args, page_no, open_browser=False)
        except Exception as e:
            return {"reply": f"Lỗi đọc PDF: {e}", "model": ""}
        pdf_html_path = str(Path("workspace") / f"pdf_trang{page_no}.html")
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
    return {"reply": saved, "model": model_key, "usage": format_usage(usage, model_key)}


def _run_chat_job(job_id, text):
    global active_job_id
    confirm_context.job_id = job_id
    try:
        with lock:
            result = _process_chat_message(text)
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
    return jsonify(pending=True, request_id=job_id), 202


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
    url = "http://127.0.0.1:5000"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=5000)