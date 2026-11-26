import json
import os
import webbrowser

import pymupdf
from llm import ask, has_foreign_chars, is_vietnamese
from project_workspace import (
    display_workspace_path,
    get_workspace_dir,
    resolve_workspace_path,
)

CHUNK = 10  # số khối gửi dịch mỗi lần gọi model
FOREIGN_WARNING = "[có thể lẫn ký tự lạ]"


def extract_pages(path):
    path = str(resolve_workspace_path(path))
    doc = pymupdf.open(path)
    return [page.get_text().strip() for page in doc]


def page_blocks(path, page_no):
    # trả về (danh sách khối chữ của trang, tổng số trang); None nếu số trang sai
    path = str(resolve_workspace_path(path))
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
    translations = data.get("translations", data.get("vi")) if isinstance(data, dict) else None
    if (
        isinstance(translations, list)
        and len(translations) == n
        and all(isinstance(x, str) for x in translations)
    ):
        return translations
    return None


def parse_read_command(args):
    tokens = args.strip().split()
    pdf_end = next(
        (i for i, token in enumerate(tokens) if token.strip("\"'").lower().endswith(".pdf")),
        None,
    )
    if pdf_end is None:
        return None
    path = " ".join(tokens[:pdf_end + 1]).strip().strip("\"'")
    if not path:
        return None
    page_no = 1
    if pdf_end + 1 < len(tokens) and tokens[pdf_end + 1].isdigit():
        page_no = int(tokens[pdf_end + 1])
    return path, page_no


def translate_blocks(gemini_client, openai_clients, blocks):
    result, model_name = [], ""
    for i in range(0, len(blocks), CHUNK):
        part = blocks[i:i + CHUNK]
        directions = [is_vietnamese(text) for text in part]
        numbered = "\n".join(
            f"[{n}] [{'Vietnamese -> English' if is_vietnamese else 'Other language -> Vietnamese'}] {text}"
            for n, (text, is_vietnamese) in enumerate(zip(part, directions), 1)
        )
        prompt = (
            "Dịch từng đoạn theo chiều ghi ngay sau số thứ tự, giữ đúng thứ tự và số lượng. "
            "Đoạn được nhận diện là tiếng Việt (trên 3% chữ cái có dấu tiếng Việt) thì dịch sang tiếng Anh; "
            "các đoạn còn lại dịch sang tiếng Việt. "
            "Giữ nguyên tên riêng, từ viết tắt và thuật ngữ (5G, AI, ...). "
            "Chỉ viết bằng tiếng Việt và tiếng Anh. Tuyệt đối không dùng chữ Thái hay ký tự của ngôn ngữ khác. "
            f"Chỉ trả về JSON hợp lệ, không dùng khối ```, dạng {{\"translations\": [...]}} gồm đúng {len(part)} chuỗi.\n\n"
            + numbered
        )
        reply, model_name, _ = ask(gemini_client, openai_clients, [], prompt,
                                   use_history=False, route_text="dịch")
        if has_foreign_chars(reply):
            reply, model_name, _ = ask(
                gemini_client, openai_clients, [], prompt,
                use_history=False, route_text="dịch",
                model_override="gemini/gemini-3.5-flash",
            )
        vi = parse_vi(reply, len(part))
        translated = vi if vi else ["(không dịch được đoạn này)"] * len(part)
        if has_foreign_chars(reply):
            translated[0] = FOREIGN_WARNING + " " + translated[0]
        result.extend(translated)
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
    return f"v2|{os.path.basename(path)}|{os.path.getsize(path)}|{page_no}"


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
.original{border-left:4px solid #2868bd;cursor:pointer}
.original:hover{background:#eef4ff}
.translation{border-left:4px solid #e0782b;cursor:pointer}
.head{font-weight:700;color:#5b6b85;background:none;border:none}
@media(max-width:640px){.row{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
<h1>__TITLE__</h1>
<p class="hint">Bấm vào đoạn văn bản để nghe đọc.</p>
<div class="row"><div class="cell head">Bản gốc</div><div class="cell head">Bản dịch</div></div>
<div id="rows"></div>
<script>
const DATA = __DATA__;
function speak(t,isVietnamese){speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(t);u.lang=isVietnamese?"vi-VN":"en-US";speechSynthesis.speak(u);}
const box = document.getElementById("rows");
DATA.forEach(p => {
  const row = document.createElement("div"); row.className = "row";
  const original = document.createElement("div"); original.className = "cell original"; original.textContent = p.original;
  original.addEventListener("click", () => speak(p.original, p.original_is_vietnamese));
  const translation = document.createElement("div"); translation.className = "cell translation"; translation.textContent = p.translation;
  translation.addEventListener("click", () => speak(p.translation, p.translation_is_vietnamese));
  row.append(original, translation); box.appendChild(row);
});
</script></div></body></html>"""


def save_pdf_html(blocks, vi, page_no, total, name, open_browser=True):
    workspace_dir = get_workspace_dir()

    def speech_is_vietnamese(text):
        if text.startswith(FOREIGN_WARNING):
            text = text[len(FOREIGN_WARNING):].lstrip()
        return is_vietnamese(text)

    pairs = [
        {
            "original": original,
            "translation": translation,
            "original_is_vietnamese": is_vietnamese(original),
            "translation_is_vietnamese": speech_is_vietnamese(translation),
        }
        for original, translation in zip(blocks, vi)
    ]
    payload = json.dumps(pairs, ensure_ascii=False).replace("</", "<\\/")
    title = f"{name[:50]} - trang {page_no}/{total}"
    html = PAGE.replace("__TITLE__", title).replace("__DATA__", payload)
    path = workspace_dir / f"pdf_trang{page_no}.html"
    path.write_text(html, encoding="utf-8")
    if open_browser:
        webbrowser.open(path.resolve().as_uri())
    return str(path)


def read_pdf_page(gemini_client, openai_clients, path, page_no, open_browser=True):
    path = str(resolve_workspace_path(path))
    display_path = display_workspace_path(path)
    if not os.path.exists(path):
        return f"Không thấy file: {display_path}", ""
    blocks, total = page_blocks(path, page_no)
    if not blocks:
        return f"Trang {page_no} không có chữ (có thể là ảnh scan).", ""
    cache = load_cache()
    key = cache_key(path, page_no)
    hit = cache.get(key)
    hit_vi = hit.get("vi", []) if isinstance(hit, dict) else []
    hit_has_foreign = any(
        has_foreign_chars(v) or v.startswith(FOREIGN_WARNING)
        for v in hit_vi if isinstance(v, str)
    )
    if hit_has_foreign:
        cache.pop(key, None)
    if (hit and not hit_has_foreign and hit.get("blocks") == blocks
            and len(hit_vi) == len(blocks)):
        vi, model_name = hit["vi"], "cache, không tốn lượt"
    else:
        vi, model_name = translate_blocks(gemini_client, openai_clients, blocks)
        if (not any(v.startswith("(không dịch được") for v in vi)
                and not any(has_foreign_chars(v) or v.startswith(FOREIGN_WARNING) for v in vi)):
            cache[key] = {"blocks": blocks, "vi": vi}
            save_cache(cache)
        elif hit_has_foreign:
            save_cache(cache)
    warning = any(v.startswith(FOREIGN_WARNING) for v in vi)
    out = save_pdf_html(
        blocks, vi, page_no, total, os.path.basename(path), open_browser=open_browser)
    display_output = display_workspace_path(out)
    message = (
        f"Đã dịch trang {page_no}/{total} ({len(blocks)} đoạn), mở {display_output}. "
        f"Xem trang khác: /read {display_path} {page_no + 1}"
    )
    if warning:
        message = FOREIGN_WARNING + " " + message
    return message, model_name