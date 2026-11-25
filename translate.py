import json
import os
import webbrowser
from pathlib import Path
from llm import ask


def build_en_prompt(text):
    return (
        "Bạn là gia sư tiếng Anh. Nếu input là tiếng Việt thì dịch sang tiếng Anh, "
        "nếu là tiếng Anh thì dịch sang tiếng Việt. Tách input thành 1 đến 6 câu.\n"
        "Chỉ trả về JSON hợp lệ, không có chữ nào ngoài JSON, không dùng khối ```. Dạng:\n"
        '{"pairs":[{"en":"English sentence","vi":"câu tiếng Việt"}],'
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

    pairs = clean(data.get("pairs"), ("en", "vi"))
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
.en{border-left:4px solid #2868bd;cursor:pointer}
.en:hover{background:#eef4ff}
.vi{border-left:4px solid #e0782b}
.head{font-weight:700;color:#5b6b85;background:none;border:none;padding:0 4px}
h2{font-size:1.05rem;margin-top:28px}
.vocab{display:flex;flex-wrap:wrap;gap:8px}
.chip{padding:8px 12px;border-radius:20px;background:#fff7e0;border:1px solid #f0d78a}
.hint{color:#5b6b85;font-size:.85rem}
@media(max-width:640px){.row{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
<h1>Dịch song ngữ Anh - Việt</h1>
<p class="hint">Bấm vào câu tiếng Anh để nghe đọc.</p>
<div class="row"><div class="cell head">English</div><div class="cell head">Tiếng Việt</div></div>
<div id="pairs"></div>
<h2>Từ vựng</h2>
<div id="vocab" class="vocab"></div>
<script>
const DATA = __DATA__;
function speak(t){
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(t);
  u.lang = "en-US";
  speechSynthesis.speak(u);
}
const pairsBox = document.getElementById("pairs");
DATA.pairs.forEach(p => {
  const row = document.createElement("div");
  row.className = "row";
  const en = document.createElement("div");
  en.className = "cell en";
  en.textContent = p.en;
  en.addEventListener("click", () => speak(p.en));
  const vi = document.createElement("div");
  vi.className = "cell vi";
  vi.textContent = p.vi;
  row.append(en, vi);
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


def save_bilingual_html(data):
    os.makedirs("workspace", exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    path = Path("workspace") / "bilingual.html"
    path.write_text(PAGE.replace("__DATA__", payload), encoding="utf-8")
    webbrowser.open(path.resolve().as_uri())
    return str(path)


def translate(gemini_client, openai_clients, text):
    reply, model_name, meta = ask(
        gemini_client, openai_clients, [], build_en_prompt(text),
        use_history=False, route_text=text,
    )
    data = parse_translation(reply)
    if data is None:
        return reply, model_name, meta
    save_bilingual_html(data)
    lines = []
    for p in data["pairs"]:
        lines.append(f"EN: {p['en']}")
        lines.append(f"VI: {p['vi']}")
    if data["vocab"]:
        lines.append("Từ vựng: " + "; ".join(f"{v['en']} = {v['vi']}" for v in data["vocab"]))
    lines.append("(Đã mở workspace/bilingual.html)")
    return "\n".join(lines), model_name, meta