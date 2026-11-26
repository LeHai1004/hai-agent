import os
from pathlib import Path

from pins import is_valid_project_name

PROJECTS_DIR = Path("history", "projects")
CURRENT_PROJECT_PATH = Path("history", "current_project.txt")


def get_current_project():
    try:
        name = CURRENT_PROJECT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "default"
    if not is_valid_project_name(name):
        return "default"
    project_dir = PROJECTS_DIR / name
    if project_dir.is_symlink() or not project_dir.is_dir():
        return "default"
    return name


def get_workspace_dir():
    project = get_current_project()
    if project == "default":
        workspace_dir = Path("workspace")
        allowed_root = Path.cwd()
    else:
        allowed_root = PROJECTS_DIR / project
        workspace_dir = allowed_root / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    resolved_workspace = workspace_dir.resolve()
    if workspace_dir.is_symlink() or not resolved_workspace.is_relative_to(allowed_root.resolve()):
        raise ValueError("Thư mục workspace không hợp lệ.")
    return resolved_workspace


def resolve_workspace_path(path, *, allow_absolute=True):
    if not isinstance(path, (str, os.PathLike)) or not os.fspath(path).strip():
        raise ValueError("Lỗi: Đường dẫn không hợp lệ.")

    raw_path = os.fspath(path).strip()
    normalized = raw_path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise ValueError("Lỗi bảo mật: Không cho phép đường dẫn có '..'.")

    root = get_workspace_dir().resolve()
    candidate = Path(raw_path)
    if candidate.is_absolute() or candidate.drive:
        if not allow_absolute:
            raise ValueError("Lỗi bảo mật: Không cho phép đường dẫn tuyệt đối.")
        target = candidate.resolve()
    else:
        if ":" in raw_path:
            raise ValueError("Lỗi bảo mật: Không cho phép đường dẫn có ổ đĩa.")
        if parts and parts[0].lower() == "workspace":
            parts = parts[1:]
        target = root.joinpath(*parts).resolve()

    if not target.is_relative_to(root):
        raise ValueError("Lỗi bảo mật: Đường dẫn nằm ngoài workspace của project hiện tại.")
    return target


def display_workspace_path(path):
    root = get_workspace_dir().resolve()
    target = resolve_workspace_path(path)
    return f"workspace/{target.relative_to(root).as_posix()}"
