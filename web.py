import os
import threading
import webbrowser
from pathlib import Path

from pins import add_pin, format_pins, load_pins, remove_pin
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from google import genai
from google.genai import types
from openai import OpenAI
from werkzeug.utils import secure_filename

from history import load_history, save_history
from llm import ask
from pdfread import read_pdf_page
from search import build_prompt, web_search
from translate import translate
from usage import format_usage, load_usage, record_usage

load_dotenv()
app = Flask(__name__)
MAX_UPLOAD_SIZE = 30 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE + 64 * 1024
lock = threading.Lock()


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
.bot{padding:4px 2px}
.meta{color:#8b8f99;font-size:.78rem;margin-top:4px}
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
<button type="button" class="ghost" id="attach" title="Chọn file PDF" aria-label="Chọn file PDF">📎</button>
<button id="send">Gửi</button></div>
<input type="file" id="pdf-file" accept=".pdf,application/pdf" hidden>
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
function addMsg(role, text, meta){
  const d = document.createElement("div");
  d.className = "msg " + (role === "user" ? "user" : "bot");
  if (role === "user") d.textContent = text; else d.innerHTML = render(text);
  if (meta){ const m = document.createElement("div"); m.className = "meta"; m.textContent = meta; d.appendChild(m); }
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
  return d;
}
async function send(){
  const text = box.value.trim();
  if (!text) return;
  box.value = "";
  addMsg("user", text);
  const wait = addMsg("bot", "Đang trả lời...");
  sendBtn.disabled = true;
  try {
    const r = await fetch("/api/chat", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({text})});
    const data = await r.json();
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
    box.value = `/read ${data.path} 1`;
    box.focus();
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
        return jsonify(error="Chọn một file PDF để tải lên."), 400
    if os.path.splitext(uploaded.filename)[1].lower() != ".pdf":
        return jsonify(error="Chỉ chấp nhận file .pdf."), 400
    filename = secure_filename(uploaded.filename)
    if not filename or os.path.splitext(filename)[1].lower() != ".pdf":
        return jsonify(error="Tên file PDF không hợp lệ."), 400
    content = uploaded.stream.read(MAX_UPLOAD_SIZE + 1)
    if len(content) > MAX_UPLOAD_SIZE:
        return jsonify(error="File PDF vượt quá giới hạn 30 MB."), 413
    os.makedirs("workspace", exist_ok=True)
    Path("workspace", filename).write_bytes(content)
    return jsonify(path=f"workspace/{filename}")


@app.get("/api/history")
def history():
    return jsonify(messages)


@app.post("/api/chat")
def chat():
    global messages
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify(error="Tin nhắn trống"), 400
    with lock:
        if text == "/new":
            messages = []
            save_history(messages)
            return jsonify(reply="Đã bắt đầu cuộc chat mới.", model="", new=True)
        if text == "/pins" or text.startswith("/pin ") or text.startswith("/unpin"):
            pins = load_pins()
            if text == "/pins":
                reply = format_pins(pins)
            elif text.startswith("/unpin"):
                reply = remove_pin(pins, text[6:].strip())
            else:
                reply = add_pin(pins, text[5:])
            return jsonify(reply=reply, model="")
        if text == "/en" or text.startswith("/en "):
            sentence = text[3:].strip()
            if not sentence:
                return jsonify(reply="Dùng: /en câu cần dịch", model="")
            try:
                reply, model_key, _ = translate(
                    gemini_client, openai_clients, sentence, open_browser=False)
            except Exception as e:
                return jsonify(reply=f"Lỗi dịch: {e}", model="")
            reply = reply.replace(
                "(Đã mở workspace/bilingual.html)",
                "Đã dịch xong. Mở bản song ngữ: /bilingual",
            )
            return jsonify(reply=reply, model=model_key)
        if text == "/read" or text.startswith("/read "):
            args = text[5:].strip()
            page_no = 1
            head, _, tail = args.rpartition(" ")
            if head and tail.isdigit():
                args, page_no = head.strip(), int(tail)
            args = args.strip('"')
            if not args:
                return jsonify(reply="Dùng: /read đường_dẫn.pdf [số trang]", model="")
            try:
                reply, model_key = read_pdf_page(
                    gemini_client, openai_clients, args, page_no, open_browser=False)
            except Exception as e:
                return jsonify(reply=f"Lỗi đọc PDF: {e}", model="")
            pdf_html_path = str(Path("workspace") / f"pdf_trang{page_no}.html")
            reply = reply.replace(pdf_html_path, f"/pdfview/{page_no}")
            return jsonify(reply=reply, model=model_key)
        question, prompt, sources = text, text, []
        if text.startswith("/web "):
            question = text[5:].strip()
            try:
                results = web_search(question)
            except Exception as e:
                return jsonify(reply=f"Lỗi tìm web: {e}", model="")
            if not results:
                return jsonify(reply="Không tìm thấy kết quả nào.", model="")
            prompt = build_prompt(question, results)
            sources = [(r.get("title", ""), r.get("href", "")) for r in results]
        try:
            answer, model_key, meta = ask(
                gemini_client, openai_clients, messages, prompt, use_history=not sources)
        except Exception as e:
            return jsonify(reply=f"Lỗi: tất cả model đều thất bại. {e}", model="")
        saved = answer
        if sources:
            saved += "\n\nNguồn:\n" + "\n".join(
                f"[{i}] {t}: {u}" for i, (t, u) in enumerate(sources, 1))
        messages.append({"role": "user", "text": question})
        messages.append({"role": "model", "text": saved})
        save_history(messages)
        record_usage(usage, model_key, meta)
        return jsonify(reply=saved, model=model_key, usage=format_usage(usage, model_key))


if __name__ == "__main__":
    url = "http://127.0.0.1:5000"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=5000)