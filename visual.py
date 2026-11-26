import json
import webbrowser
from pathlib import Path
from llm import ask
from project_workspace import get_workspace_dir


def build_vis_prompt(text):
    return (
        "Bạn là chuyên gia kiến trúc hệ thống và luồng dữ liệu.\n"
        "Hãy phân tích câu hỏi hoặc đoạn code sau và trả về bằng tiếng Việt, ngắn gọn, súc tích, tuân thủ đúng 4 phần sau:\n\n"
        "1. Bức tranh toàn cảnh: kể cả quá trình từ lúc bấm/chạy đến lúc ra kết quả, gồm cả phần người hỏi chưa hỏi (biên dịch, JVM, RAM, CPU, ổ đĩa, mạng nếu có). Nếu không chắc thì ghi 'chưa chắc', không bịa.\n"
        "2. Một sơ đồ Mermaid sequenceDiagram minh họa luồng hoạt động:\n"
        "```mermaid\n"
        "sequenceDiagram\n"
        "    ... (vẽ sơ đồ các thành phần)\n"
        "```\n"
        "3. Các bước đánh số chi tiết, mỗi bước ghi rõ dữ liệu đang nằm ở đâu (RAM, ổ đĩa hay mạng).\n"
        "4. 'Có thể bạn chưa biết': 3 câu nên hỏi tiếp, mỗi câu kèm trả lời 1 dòng.\n\n"
        "Với ngôn ngữ cần biên dịch (Java, C#...), luôn nêu đủ chuỗi: ai biên dịch, sinh ra file gì, lưu ở đâu, ai nạp vào RAM, ai chạy; không bỏ bước nào và không viết 'RAM hoặc ổ đĩa' cho một bước.\n"
        "Cuối câu trả lời, bổ sung một khối ```json hợp lệ (không có nội dung nào sau khối này). "
        "actors là danh sách tên các thành phần; mỗi step có from và to là tên actor, "
        "text là đúng một câu mô tả bước, data là nơi dữ liệu hiện đang nằm và chỉ chọn "
        "'RAM', 'ổ đĩa' hoặc 'mạng'. Không thêm trường khác:\n"
        "{\n"
        '  "actors": ["Ổ đĩa", "RAM", "CPU", "Mạng"],\n'
        '  "steps": [\n'
        '    {"from": "Ổ đĩa", "to": "RAM", "text": "Đọc file code từ ổ đĩa vào RAM", "data": "RAM"},\n'
        '    {"from": "RAM", "to": "CPU", "text": "CPU nạp lệnh từ RAM để thực thi.", "data": "RAM"}\n'
        "  ]\n"
        "}\n\n"
        f"Input: {text}"
    )


def save_workspace_vis(content):
    path = get_workspace_dir() / "vis.md"
    mermaid_code = ""
    if "```mermaid" in content:
        parts = content.split("```mermaid")
        if len(parts) > 1:
            m_part = parts[1].split("```")[0].strip()
            mermaid_code = "```mermaid\n" + m_part + "\n```"
    
    with open(path, "w", encoding="utf-8") as f:
        f.write(mermaid_code if mermaid_code else content)
    return path


def save_workspace_html(content, open_browser=True):
    html_path = get_workspace_dir() / "vis.html"

    json_data = None
    try:
        json_parts = content.rsplit("```json", 1)[1].split("```", 1)
        if len(json_parts) != 2 or json_parts[1].strip():
            raise ValueError("JSON block must be final")
        json_str = json_parts[0].strip()
        json_data = json.loads(json_str)
        if not isinstance(json_data, dict):
            raise ValueError("JSON root must be an object")

        actors = json_data.get("actors")
        steps = json_data.get("steps")
        if (
            not isinstance(actors, list)
            or not actors
            or any(not isinstance(actor, str) or not actor.strip() for actor in actors)
            or len(set(actors)) != len(actors)
            or not isinstance(steps, list)
            or not steps
        ):
            raise ValueError("Invalid actors or steps")

        for step in steps:
            if (
                not isinstance(step, dict)
                or not all(
                    isinstance(step.get(key), str) and step[key].strip()
                    for key in ("from", "to", "text", "data")
                )
                or step["from"] not in actors
                or step["to"] not in actors
                or step["data"].strip().lower() not in ("ram", "ổ đĩa", "mạng")
            ):
                raise ValueError("Invalid step")
    except (IndexError, json.JSONDecodeError, TypeError, ValueError):
        print("Không tạo được hoạt hình")
        return None

    html_template = '''<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Hoạt hình luồng dữ liệu</title>
    <style>
        body { margin: 0; padding: 24px; color: #172033; background: #f3f6fb; font-family: sans-serif; text-align: center; }
        h1 { font-size: 1.35rem; }
        #stage { position: relative; max-width: 1100px; margin: 36px auto 20px; overflow-x: auto; border: 1px solid #d5deeb; border-radius: 12px; background: #fff; }
        #actors-container { display: flex; align-items: center; justify-content: space-around; gap: 28px; width: max-content; min-width: 100%; box-sizing: border-box; padding: 54px 32px; }
        .actor { position: relative; z-index: 1; flex: 0 0 auto; min-width: 100px; padding: 16px 22px; border: 2px solid #3978d4; border-radius: 10px; background: #fff; color: #245ba9; font-weight: 700; box-shadow: 0 4px 12px #20385c18; }
        #animation-layer { position: absolute; z-index: 2; inset: 0; width: 100%; height: 100%; overflow: visible; pointer-events: none; }
        #particle { fill: #ff5722; filter: drop-shadow(0 0 7px #ff5722); transition: cx 650ms ease-in-out, cy 650ms ease-in-out; }
        #info { min-height: 3.5em; margin: 18px auto; font-size: 1rem; line-height: 1.6; }
        #data-location { color: #b42362; font-size: .95rem; }
        .controls { display: flex; justify-content: center; gap: 10px; }
        button { padding: 10px 18px; border: 0; border-radius: 7px; background: #2868bd; color: white; font-size: .95rem; cursor: pointer; }
        button:hover { background: #1d4f91; }
        button:disabled { background: #aab4c2; cursor: not-allowed; }
        @media (max-width: 600px) { body { padding: 16px 10px; } }
    </style>
</head>
<body>
    <h1>Hoạt hình luồng dữ liệu hệ thống</h1>
    <div id="stage">
        <div id="actors-container"></div>
        <svg id="animation-layer" aria-hidden="true"><circle id="particle" r="10" cx="0" cy="0"></circle></svg>
    </div>
    <div id="info" aria-live="polite"></div>
    <div class="controls">
        <button id="prev-btn" type="button">Trước</button>
        <button id="next-btn" type="button">Sau</button>
        <button id="auto-btn" type="button">Tự chạy</button>
    </div>

    <script>
        const data = __VIS_DATA_JSON__;
        let currentStep = 0;
        let timer = null;

        const stage = document.getElementById("stage");
        const actorsContainer = document.getElementById("actors-container");
        const infoDiv = document.getElementById("info");
        const particle = document.getElementById("particle");
        const autoBtn = document.getElementById("auto-btn");
        const prevBtn = document.getElementById("prev-btn");
        const nextBtn = document.getElementById("next-btn");

        data.actors.forEach(name => {
            const div = document.createElement("div");
            div.className = "actor";
            div.dataset.actor = name;
            div.textContent = name;
            actorsContainer.appendChild(div);
        });

        function updateStage() {
            const step = data.steps[currentStep];
            infoDiv.replaceChildren();
            const description = document.createElement("div");
            description.textContent = "Bước " + (currentStep + 1) + "/" + data.steps.length + ": " + step.text;
            const location = document.createElement("div");
            location.id = "data-location";
            location.textContent = "Dữ liệu hiện ở: " + step.data;
            infoDiv.append(description, location);

            const fromEl = Array.from(actorsContainer.children)
                .find(actor => actor.dataset.actor === step.from);
            const toEl = Array.from(actorsContainer.children)
                .find(actor => actor.dataset.actor === step.to);
                        const startX = fromEl.offsetLeft + fromEl.offsetWidth / 2;
            const startY = fromEl.offsetTop + fromEl.offsetHeight / 2;
            const endX = toEl.offsetLeft + toEl.offsetWidth / 2;
            const endY = toEl.offsetTop + toEl.offsetHeight / 2;
            stage.scrollTo({ left: Math.max(0, endX - stage.clientWidth / 2), behavior: "smooth" });

            particle.style.transition = "none";
            particle.setAttribute("cx", startX);
            particle.setAttribute("cy", startY);
            particle.getBoundingClientRect();
            particle.style.transition = "";
            requestAnimationFrame(() => {
                particle.setAttribute("cx", endX);
                particle.setAttribute("cy", endY);
            });
            prevBtn.disabled = currentStep === 0;
            nextBtn.disabled = currentStep === data.steps.length - 1;
        }

        function nextStep() {
            if (currentStep < data.steps.length - 1) {
                currentStep++;
                updateStage();
            }
        }

        function prevStep() {
            if (currentStep > 0) {
                currentStep--;
                updateStage();
            }
        }

        function toggleAuto() {
            if (timer) {
                stopAuto();
            } else {
                if (currentStep === data.steps.length - 1) {
                    currentStep = 0;
                    updateStage();
                }
                autoBtn.innerText = "Dừng";
                timer = setInterval(() => {
                    if (currentStep < data.steps.length - 1) {
                        currentStep++;
                        updateStage();
                    } else {
                        stopAuto();
                    }
                }, 1500);
            }
        }

        function stopAuto() {
            if (timer) {
                clearInterval(timer);
                timer = null;
                autoBtn.innerText = "Tự chạy";
            }
        }

        prevBtn.addEventListener("click", prevStep);
        nextBtn.addEventListener("click", nextStep);
        autoBtn.addEventListener("click", toggleAuto);
        window.addEventListener("resize", updateStage);
        window.onload = () => {
            updateStage();
        };
    </script>
</body>
</html>'''

    data_json = json.dumps(json_data, ensure_ascii=False).replace("</", "<\\/")
    html_content = html_template.replace("__VIS_DATA_JSON__", data_json)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    if open_browser:
        webbrowser.open(Path(html_path).resolve().as_uri())
    return html_path

def build_scenes_prompt(text):
    return (
        "Bạn là người làm video giải thích kiểu TikTok cho sinh viên CNTT.\n"
        "Chia nội dung sau thành 5 đến 8 cảnh nối tiếp, mỗi cảnh một ý duy nhất. "
        "Chỉ trả về JSON hợp lệ, không có chữ nào ngoài JSON, không dùng khối ```. Dạng:\n"
        '{"scenes":[{"title":"...","layout":"stack","items":["..."],"hl":0,'
        '"caption":"... **từ khóa** ...","say":"..."}]}\n'
        "Quy tắc:\n"
        "- title: tiếng Việt in hoa, tối đa 6 từ.\n"
        "- layout: 'stack' (khối xếp dọc, dùng cho tầng/lớp/thứ tự) hoặc "
        "'row' (khối xếp ngang, dùng cho các phần ghép thành một gói).\n"
        "- items: 2 đến 7 khối, mỗi khối tối đa 3 từ.\n"
        "- hl: số thứ tự (từ 0) của khối cần nhấn mạnh; bỏ trường này nếu không cần.\n"
        "- caption: một câu dưới 12 từ, bọc đúng một từ khóa bằng **.\n"
        "- say: 1 đến 2 câu tiếng Việt như đang nói chuyện, dễ hiểu, không dùng ký hiệu markdown.\n"
        "- Đi từ bức tranh lớn đến chi tiết, gồm cả phần người học chưa biết để hỏi. "
        "Cảnh cuối là 'CÓ THỂ BẠN CHƯA BIẾT' với một điều nên hỏi tiếp.\n"
        "- Không chắc thì bỏ, không bịa.\n\n"
        f"Chủ đề: {text}"
    )


def parse_scenes(content):
    text = content.strip().strip("`").strip()
    if text.lower().startswith("json"):
        text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    scenes = data.get("scenes") if isinstance(data, dict) else None
    if not isinstance(scenes, list) or not scenes:
        return None
    clean = []
    for s in scenes:
        if not isinstance(s, dict):
            return None
        items = s.get("items")
        if not all(isinstance(s.get(k), str) and s[k].strip() for k in ("title", "caption", "say")):
            return None
        if not isinstance(items, list) or not 1 <= len(items) <= 8 or any(not isinstance(i, str) for i in items):
            return None
        scene = {
            "title": s["title"],
            "layout": "stack" if s.get("layout") == "stack" else "row",
            "items": items,
            "caption": s["caption"],
            "say": s["say"],
        }
        hl = s.get("hl")
        if isinstance(hl, int) and not isinstance(hl, bool) and 0 <= hl < len(items):
            scene["hl"] = hl
        clean.append(scene)
    return clean


def save_scenes_html(scenes, open_browser=True):
    template = Path("vis_player.html")
    if not template.exists():
        print("Thiếu file vis_player.html")
        return None
    workspace_dir = get_workspace_dir()
    data = json.dumps(scenes, ensure_ascii=False).replace("</", "<\\/")
    html = template.read_text(encoding="utf-8").replace(
        "<script>",
        "<script>window.SCENES_OVERRIDE = " + data + ";</script>\n<script>",
        1,
    )
    path = workspace_dir / "vis_play.html"
    path.write_text(html, encoding="utf-8")
    if open_browser:
        webbrowser.open(path.resolve().as_uri())
    return str(path)

def play(gemini_client, openai_clients, text, open_browser=True):
    prompt = build_scenes_prompt(text)
    reply, model_name, meta = ask(gemini_client, openai_clients, [], prompt, use_history=False, route_text=text)
    scenes = parse_scenes(reply)
    if scenes is None:
        print("Không đọc được JSON cảnh từ model.")
    else:
        save_scenes_html(scenes, open_browser=open_browser)
    return reply, model_name, meta

def visualize(gemini_client, openai_clients, text, open_browser=True):
    prompt = build_vis_prompt(text)
    reply, model_name, meta = ask(gemini_client, openai_clients, [], prompt, use_history=False, route_text=text)
    save_workspace_vis(reply)
    save_workspace_html(reply, open_browser=open_browser)
    return reply, model_name, meta
