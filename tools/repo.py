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

if __name__ == "__main__":
    print(scan_repo(sys.argv[1] if len(sys.argv) > 1 else "."))