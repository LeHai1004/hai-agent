import json
import webbrowser
from llm import ask, has_foreign_chars, is_vietnamese
from project_workspace import get_workspace_dir

FOREIGN_WARNING = "[có thể lẫn ký tự lạ]"


def build_en_prompt(text):
    detected_language = "tiếng Việt" if is_vietnamese(text) else "không phải tiếng Việt"
    return (
        "Bạn là gia sư song ngữ Anh - Việt. Đoạn input được bộ nhận diện xếp là "
        f"{detected_language}; nếu input có nhiều câu hoặc đoạn thì phân loại từng câu/đoạn riêng. "
        "Nếu trên 3% chữ cái trong đoạn là ký tự có dấu tiếng Việt thì dịch sang tiếng Anh; "
        "ngược lại dịch sang tiếng Việt. Tách input thành 1 đến 6 câu.\n"
        "Trong mỗi cặp, original phải giữ nguyên văn bản gốc và translation là bản dịch.\n"
        "Chỉ viết bằng tiếng Việt và tiếng Anh. Tuyệt đối không dùng chữ Thái hay ký tự của ngôn ngữ khác.\n"
        "Chỉ trả về JSON hợp lệ, không có chữ nào ngoài JSON, không dùng khối ```. Dạng:\n"
        '{"pairs":[{"original":"văn bản gốc","translation":"bản dịch"}],'
        '"vocab":[{"en":"word or phrase","vi":"nghĩa"}]}\n'
        "vocab: 3 đến 5 từ hoặc cụm quan trọng nhất. Không chắc nghĩa thì bỏ, không bịa.\n\n"
        f"Input: {text}"
    )


def parse_translation(content):
    text = content.strip().strip("`").strip()
    if text.lower().startswith("json"):
        text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    def clean(items, keys):
        out = []
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and all(isinstance(it.get(k), str) and it[k].strip() for k in keys):
                out.append({k: it[k].strip() for k in keys})
        return out

    pairs = clean(data.get("pairs"), ("original", "translation"))
    if not pairs:
        return None
    return {"pairs": pairs, "vocab": clean(data.get("vocab"), ("en", "vi"))}


PAGE = """<!DOCTYPE html>
<html lang="vi"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dịch song ngữ</title>
<style>
body{margin:0;padding:24px;background:#f3f6fb;color:#172033;font-family:sans-serif}
.wrap{max-width:900px;margin:auto}
h1{font-size:1.3rem}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.cell{padding:14px 16px;border-radius:10px;background:#fff;border:1px solid #d5deeb;line-height:1.5}
.original{border-left:4px solid #2868bd;cursor:pointer}
.original:hover{background:#eef4ff}
.translation{border-left:4px solid #e0782b;cursor:pointer}
.head{font-weight:700;color:#5b6b85;background:none;border:none;padding:0 4px}
h2{font-size:1.05rem;margin-top:28px}
.vocab{display:flex;flex-wrap:wrap;gap:8px}
.chip{padding:8px 12px;border-radius:20px;background:#fff7e0;border:1px solid #f0d78a}
.hint{color:#5b6b85;font-size:.85rem}
@media(max-width:640px){.row{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
<h1>Dịch song ngữ Anh - Việt</h1>
<p class="hint">Bấm vào đoạn văn bản để nghe đọc.</p>
<div class="row"><div class="cell head">Bản gốc</div><div class="cell head">Bản dịch</div></div>
<div id="pairs"></div>
<h2>Từ vựng</h2>
<div id="vocab" class="vocab"></div>
<script>
const DATA = __DATA__;
function speak(t, isVietnamese){
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(t);
  u.lang = isVietnamese ? "vi-VN" : "en-US";
  speechSynthesis.speak(u);
}
const pairsBox = document.getElementById("pairs");
DATA.pairs.forEach(p => {
  const row = document.createElement("div");
  row.className = "row";
  const original = document.createElement("div");
  original.className = "cell original";
  original.textContent = p.original;
  original.addEventListener("click", () => speak(p.original, p.original_is_vietnamese));
  const translation = document.createElement("div");
  translation.className = "cell translation";
  translation.textContent = p.translation;
  translation.addEventListener("click", () => speak(p.translation, p.translation_is_vietnamese));
  row.append(original, translation);
  pairsBox.appendChild(row);
});
const vocabBox = document.getElementById("vocab");
DATA.vocab.forEach(v => {
  const chip = document.createElement("span");
  chip.className = "chip";
  chip.textContent = v.en + " = " + v.vi;
  vocabBox.appendChild(chip);
});
</script></div></body></html>"""


def save_bilingual_html(data, open_browser=True):
    workspace_dir = get_workspace_dir()

    def speech_is_vietnamese(text):
        if text.startswith(FOREIGN_WARNING):
            text = text[len(FOREIGN_WARNING):].lstrip()
        return is_vietnamese(text)

    rendered = {
        "pairs": [
            {
                **pair,
                "original_is_vietnamese": is_vietnamese(pair["original"]),
                "translation_is_vietnamese": speech_is_vietnamese(pair["translation"]),
            }
            for pair in data["pairs"]
        ],
        "vocab": data["vocab"],
    }
    payload = json.dumps(rendered, ensure_ascii=False).replace("</", "<\\/")
    path = workspace_dir / "bilingual.html"
    path.write_text(PAGE.replace("__DATA__", payload), encoding="utf-8")
    if open_browser:
        webbrowser.open(path.resolve().as_uri())
    return str(path)


def translate(gemini_client, openai_clients, text, open_browser=True):
    prompt = build_en_prompt(text)
    reply, model_name, meta = ask(
        gemini_client, openai_clients, [], prompt,
        use_history=False, route_text=text,
    )
    if has_foreign_chars(reply):
        reply, model_name, meta = ask(
            gemini_client, openai_clients, [], prompt,
            use_history=False, route_text=text,
            model_override="gemini/gemini-3.5-flash",
        )
    warning = has_foreign_chars(reply)
    data = parse_translation(reply)
    if data is None:
        if warning:
            reply = FOREIGN_WARNING + " " + reply
        return reply, model_name, meta
    if warning:
        data["pairs"][0]["translation"] = (
            FOREIGN_WARNING + " " + data["pairs"][0]["translation"]
        )
    output_path = save_bilingual_html(data, open_browser=open_browser)
    lines = []
    if warning:
        lines.append(FOREIGN_WARNING)
    for p in data["pairs"]:
        lines.append(f"Bản gốc: {p['original']}")
        lines.append(f"Bản dịch: {p['translation']}")
    if data["vocab"]:
        lines.append("Từ vựng: " + "; ".join(f"{v['en']} = {v['vi']}" for v in data["vocab"]))
    lines.append(f"(Đã mở {output_path})")
    return "\n".join(lines), model_name, meta