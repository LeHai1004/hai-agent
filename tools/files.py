import os
from pathlib import Path

WORKSPACE_DIR = (Path.cwd() / "workspace").resolve()
WORKSPACE_DIR.mkdir(exist_ok=True)


def _get_safe_path(path_str: str):
    """Resolve path and verify it stays inside WORKSPACE_DIR."""
    if not path_str or not path_str.strip():
        return None, "Lỗi: Đường dẫn không hợp lệ."

    # Block suspicious path patterns
    if ".." in path_str or os.path.isabs(path_str) or ":" in path_str:
        return None, "Lỗi bảo mật: Không cho phép đường dẫn tuyệt đối, ổ đĩa hoặc '..' để truy cập ngoài workspace/."

    try:
        target = (WORKSPACE_DIR / path_str).resolve()
        if not target.is_relative_to(WORKSPACE_DIR):
            return None, "Lỗi bảo mật: Đường dẫn nằm ngoài thư mục workspace/."
        return target, None
    except Exception as e:
        return None, f"Lỗi xử lý đường dẫn: {e}"


def list_files() -> str:
    """List all files and directories inside workspace/."""
    print("  [tool] list_files")
    if not WORKSPACE_DIR.exists():
        return "Workspace directory is empty."

    items = []
    for item in WORKSPACE_DIR.rglob("*"):
        rel_path = item.relative_to(WORKSPACE_DIR)
        kind = "dir" if item.is_dir() else "file"
        items.append(f"- {rel_path} ({kind})")

    if not items:
        return "Workspace is empty."
    return "\n".join(items)


def read_file(path: str) -> str:
    """Read content of a text file inside workspace/ (up to 20,000 characters)."""
    print(f"  [tool] read_file: {path}")
    target, err = _get_safe_path(path)
    if err:
        return err

    if not target.exists():
        return f"Lỗi: File '{path}' không tồn tại."
    if not target.is_file():
        return f"Lỗi: '{path}' không phải là file."

    try:
        content = target.read_text(encoding="utf-8")
        if len(content) > 20000:
            content = content[:20000] + "\n...[Nội dung bị cắt do vượt quá 20.000 ký tự]"
        return content
    except Exception as e:
        return f"Lỗi đọc file: {e}"


def write_file(path: str, content: str) -> str:
    """Write text content to a file inside workspace/ after user confirmation."""
    print(f"  [tool] write_file: {path}")
    target, err = _get_safe_path(path)
    if err:
        return err

    preview = content[:200] + ("..." if len(content) > 200 else "")
    note = "file đã tồn tại, sẽ GHI ĐÈ" if target.exists() else "file mới"
    print(f"\nYêu cầu ghi file: {path} ({note})")
    print(f"Nội dung xem trước (200 ký tự đầu):\n{preview}")
    ans = input("Cho phép ghi? (y/n): ").strip().lower()
    if ans != "y":
        return "Đã hủy ghi file theo yêu cầu người dùng (Cancelled by user)."

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Đã ghi file '{path}' thành công."
    except Exception as e:
        return f"Lỗi ghi file: {e}"
