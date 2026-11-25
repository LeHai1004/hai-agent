"""Quét một thư mục dự án: liệt kê file, import, class và hàm cấp cao nhất."""
import ast
import sys
from pathlib import Path

BO_QUA = {".venv", "__pycache__", ".git", "history", "exports", "workspace", "node_modules"}


def _phan_tich_py(file: Path):
    try:
        cay = ast.parse(file.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None
    imports = set()
    for node in ast.walk(cay):
        if isinstance(node, ast.Import):
            imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    classes = [n.name for n in cay.body if isinstance(n, ast.ClassDef)]
    funcs = [n.name for n in cay.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return sorted(imports), classes, funcs


def scan_repo(root: str = ".") -> str:
    goc = Path(root).resolve()
    cac_file = []
    for file in sorted(goc.rglob("*")):
        phan = file.relative_to(goc).parts
        if file.is_file() and not any(p in BO_QUA for p in phan):
            cac_file.append((file, phan))

    # tên module nội bộ = tên file .py và tên thư mục chứa chúng
    noi_bo = set()
    for file, phan in cac_file:
        if file.suffix == ".py":
            noi_bo.add(file.stem)
            if len(phan) > 1:
                noi_bo.add(phan[-2])

    dong = [f"Dự án: {goc.name}"]
    for file, phan in cac_file:
        ten = "/".join(phan)
        if file.suffix != ".py":
            dong.append(f"- {ten}")
            continue
        kq = _phan_tich_py(file)
        if kq is None:
            dong.append(f"- {ten} (không đọc được)")
            continue
        imports, classes, funcs = kq
        dong.append(f"- {ten}")
        trong = [i for i in imports if i in noi_bo]
        ngoai = [i for i in imports if i not in noi_bo]
        if trong:
            dong.append(f"    gọi file nội bộ: {', '.join(trong)}")
        if ngoai:
            dong.append(f"    thư viện ngoài: {', '.join(ngoai)}")
        if classes:
            dong.append(f"    class: {', '.join(classes)}")
        if funcs:
            dong.append(f"    hàm: {', '.join(funcs)}")
    return "\n".join(dong)


def read_repo(path: str) -> str:
    """Scan a project folder and list its files, imports, classes and functions.
    Use path "." for the whole current project."""
    print(f"  [tool] read_repo {path}")
    if not Path(path).is_dir():
        return f"Không tìm thấy thư mục: {path}"
    kq = scan_repo(path)
    if len(kq) > 8000:
        kq = kq[:8000] + "\n... (đã cắt bớt vì quá dài)"
    return kq

def _id(ten: str) -> str:
    return ten.replace("/", "_").replace(".", "_")


def draw_diagram(path: str = ".") -> str:
    """Draw a Mermaid diagram of which project file imports which file.
    Saves it to workspace/diagram.md. Use path "." for the whole project."""
    print(f"  [tool] draw_diagram {path}")
    goc = Path(path).resolve()
    if not goc.is_dir():
        return f"Không tìm thấy thư mục: {path}"
    files = [f for f in sorted(goc.rglob("*.py"))
             if not f.name.startswith("test_")
             and not any(p in BO_QUA for p in f.relative_to(goc).parts)]

    ten_module = {}
    for f in files:
        rel = f.relative_to(goc).as_posix()
        if f.name == "__init__.py":
            if f.parent != goc:
                ten_module[f.parent.name] = rel
        else:
            ten_module.setdefault(f.stem, rel)

    canh = set()
    for f in files:
        rel = f.relative_to(goc).as_posix()
        kq = _phan_tich_py(f)
        if not kq:
            continue
        for ten in kq[0]:
            dich = ten_module.get(ten)
            if dich and dich != rel:
                canh.add((rel, dich))

    dong = ["flowchart LR"]
    groups = (
        ("ui", "Giao diện", {"chat.py", "web.py"}),
        ("core", "Lõi", {"llm.py", "history.py", "usage.py", "pins.py", "config.py"}),
        ("features", "Tính năng", {
            "translate.py", "pdfread.py", "img.py", "filechat.py", "visual.py",
            "gitcoach.py", "transfer.py", "search.py",
        }),
    )
    grouped = set()
    for group_id, label, names in groups:
        group_files = [
            (f, f.relative_to(goc).as_posix())
            for f in files
            if f.relative_to(goc).as_posix() in names
        ]
        if group_files:
            dong.append(f'    subgraph {group_id}["{label}"]')
            for f, rel in group_files:
                dong.append(f'        {_id(rel)}["{rel}"]')
                grouped.add(rel)
            dong.append("    end")

    tool_files = [
        (f, f.relative_to(goc).as_posix())
        for f in files
        if f.relative_to(goc).parts[0] == "tools"
    ]
    if tool_files:
        dong.append('    subgraph tools["Tools"]')
        for f, rel in tool_files:
            dong.append(f'        {_id(rel)}["{rel}"]')
            grouped.add(rel)
        dong.append("    end")

    for f in files:
        rel = f.relative_to(goc).as_posix()
        if rel not in grouped:
            dong.append(f'    {_id(rel)}["{rel}"]')
    for a, b in sorted(canh):
        dong.append(f"    {_id(a)} --> {_id(b)}")
    code = "\n".join(dong)

    out = Path("workspace")
    out.mkdir(exist_ok=True)
    (out / "diagram.md").write_text(f"```mermaid\n{code}\n```\n", encoding="utf-8")
    return f"Đã lưu workspace/diagram.md. Mã Mermaid:\n{code}"

if __name__ == "__main__":
    print(scan_repo(sys.argv[1] if len(sys.argv) > 1 else "."))