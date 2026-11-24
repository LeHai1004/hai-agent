import json
from visual import parse_scenes, save_scenes_html

raw = json.dumps({"scenes": [
    {"title": "THỬ NGHIỆM", "layout": "row", "items": ["A", "B", "C"], "hl": 1,
     "caption": "Khối **B** sáng lên", "say": "Đây là cảnh thử nghiệm."}
]}, ensure_ascii=False)

save_scenes_html(parse_scenes(raw))