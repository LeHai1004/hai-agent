from docx import Document

from .files import _get_safe_path


def create_word(path: str, title: str, content: str) -> str:
    """Create a Word (.docx) document inside the workspace folder. path is
    relative to workspace and must end with .docx. content is plain text:
    a line starting with '# ' becomes a heading, a line starting with '- '
    becomes a bullet, any other line becomes a paragraph. Do not repeat the
    title inside content. The user is asked
    to confirm before the file is created."""
    print(f"  [tool] create_word: {path}")
    if not path.lower().endswith(".docx"):
        return "Lỗi: tên file phải kết thúc bằng .docx."
    target, error = _get_safe_path(path)
    if error:
        return error

    note = "file đã tồn tại, sẽ GHI ĐÈ" if target.exists() else "file mới"
    print(f"\nYêu cầu tạo Word: {path} ({note})")
    print(f"Tiêu đề: {title}")
    print(f"Nội dung xem trước (200 ký tự đầu):\n{content[:200]}")
    from . import ConfirmationTimeoutError, confirm

    try:
        approved = confirm(
            f"Tạo file Word '{path}' ({note})",
            tool_name="create_word",
        )
    except ConfirmationTimeoutError as e:
        return f"Lỗi xác nhận: {e}"
    if not approved:
        return "Người dùng đã từ chối tạo file."

    try:
        doc = Document()
        doc.add_heading(title, level=0)
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("# "):
                doc.add_heading(line[2:], level=1)
            elif line.startswith("- "):
                doc.add_paragraph(line[2:], style="List Bullet")
            else:
                doc.add_paragraph(line)
        target.parent.mkdir(parents=True, exist_ok=True)
        doc.save(target)
        return f"Đã tạo file Word '{path}'."
    except Exception as e:
        return f"Lỗi tạo file Word: {e}"