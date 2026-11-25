import json
import os
import webbrowser
from pathlib import Path

import pymupdf
from llm import ask

CHUNK = 10  # số khối gửi dịch mỗi lần gọi model


def extract_pages(path):
    doc = pymupdf.open(path)
    return [page.get_text().strip() for page in doc]


def page_blocks(path, page_no):
    # trả về (danh sách khối chữ của trang, tổng số trang); None nếu số trang sai
    doc = pymupdf.open(path)
    total = len(doc)
    if not 1 <= page_no <= total:
        return None, total
    blocks = []
    for b in doc[page_no - 1].get_text("blocks", sort=True):
        if b[6] != 0:  # bỏ khối ảnh
            continue
        text = " ".join(b[4].split())
        if len(text) >= 3:
            blocks.append(text)
    return blocks, total


def parse_vi(reply, n):
    text = reply.strip().strip("`").strip()
    if text.lower().startswith("json"):
        text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    vi = data.get("vi") if isinstance(data, dict) else None
    if isinstance(vi, list) and len(vi) == n and all(isinstance(x, str) for x in vi):
        return vi
    return None


def translate_blocks(gemini_client, openai_clients, blocks):
    result, model_name = [], ""
    for i in range(0, len(blocks), CHUNK):
        part = blocks[i:i + CHUNK]
        numbered = "\n".join(f"[{n}] {t}" for n, t in enumerate(part, 1))
        prompt = (
            "Dịch từng đoạn tiếng Anh sau sang tiếng Việt, giữ đúng thứ tự và đúng số lượng đoạn. "
            "Giữ nguyên tên riêng, từ viết tắt và thuật ngữ (5G, AI, ...). "
            f"Chỉ trả về JSON hợp lệ, không dùng khối ```, dạng {{\"vi\": [...]}} gồm đúng {len(part)} chuỗi.\n\n"
            + numbered
        )
        reply, model_name, _ = ask(gemini_client, openai_clients, [], prompt,
                                   use_history=False, route_text="dịch")
        vi = parse_vi(reply, len(part))
        result.extend(vi if vi else ["(không dịch được đoạn này)"] * len(part))
    return result, model_name

CACHE_PATH = os.path.join("history", "pdf_cache.json")


def load_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    os.makedirs("history", exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def cache_key(path, page_no):
    # đổi file (dung lượng khác) thì cache cũ tự không khớp
    return f"{os.path.basename(path)}|{os.path.getsize(path)}|{page_no}"

PAGE = """<!DOCTYPE html>
<html lang="vi"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
body{margin:0;padding:24px;background:#f3f6fb;color:#172033;font-family:sans-serif}
.wrap{max-width:1000px;margin:auto}
h1{font-size:1.2rem}
.hint{color:#5b6b85;font-size:.85rem}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:10px}
.cell{padding:12px 14px;border-radius:10px;background:#fff;border:1px solid #d5deeb;line-height:1.5;font-size:.95rem}
.en{border-left:4px solid #2868bd;cursor:pointer}
.en:hover{background:#eef4ff}
.vi{border-left:4px solid #e0782b}
.head{font-weight:700;color:#5b6b85;background:none;border:none}
@media(max-width:640px){.row{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
<h1>__TITLE__</h1>
<p class="hint">Bấm vào đoạn tiếng Anh để nghe đọc.</p>
<div class="row"><div class="cell head">English</div><div class="cell head">Tiếng Việt</div></div>
<div id="rows"></div>
<script>
const DATA = __DATA__;
function speak(t){speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(t);u.lang="en-US";speechSynthesis.speak(u);}
const box = document.getElementById("rows");
DATA.forEach(p => {
  const row = document.createElement("div"); row.className = "row";
  const en = document.createElement("div"); en.className = "cell en"; en.textContent = p.en;
  en.addEventListener("click", () => speak(p.en));
  const vi = document.createElement("div"); vi.className = "cell vi"; vi.textContent = p.vi;
  row.append(en, vi); box.appendChild(row);
});
</script></div></body></html>"""


def save_pdf_html(blocks, vi, page_no, total, name, open_browser=True):
    os.makedirs("workspace", exist_ok=True)
    pairs = [{"en": e, "vi": v} for e, v in zip(blocks, vi)]
    payload = json.dumps(pairs, ensure_ascii=False).replace("</", "<\\/")
    title = f"{name} - trang {page_no}/{total}"
    html = PAGE.replace("__TITLE__", title).replace("__DATA__", payload)
    path = Path("workspace") / f"pdf_trang{page_no}.html"
    path.write_text(html, encoding="utf-8")
    if open_browser:
        webbrowser.open(path.resolve().as_uri())
    return str(path)


def read_pdf_page(gemini_client, openai_clients, path, page_no, open_browser=True):
    if not os.path.exists(path):
        return f"Không thấy file: {path}", ""
    blocks, total = page_blocks(path, page_no)
    if not blocks:
        return f"Trang {page_no} không có chữ (có thể là ảnh scan).", ""
    cache = load_cache()
    key = cache_key(path, page_no)
    hit = cache.get(key)
    if hit and hit.get("blocks") == blocks and len(hit.get("vi", [])) == len(blocks):
        vi, model_name = hit["vi"], "cache, không tốn lượt"
    else:
        vi, model_name = translate_blocks(gemini_client, openai_clients, blocks)
        if not any(v.startswith("(không dịch được") for v in vi):
            cache[key] = {"blocks": blocks, "vi": vi}
            save_cache(cache)
    out = save_pdf_html(
        blocks, vi, page_no, total, os.path.basename(path), open_browser=open_browser)
    return f"Đã dịch trang {page_no}/{total} ({len(blocks)} đoạn), mở {out}. Xem trang khác: /read {path} {page_no + 1}", model_name