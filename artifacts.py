import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pins import is_valid_project_name
from storage import read_json, update_json


PROJECTS_DIR = Path("history", "projects")
CURRENT_PROJECT_PATH = Path("history", "current_project.txt")
ARTIFACT_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
MAX_CONTENT_LENGTH = 200_000
MAX_VERSIONS = 20
MAX_TITLE_LENGTH = 200
MAX_LANGUAGE_LENGTH = 40


class ArtifactVersionConflict(ValueError):
    pass


def _line_body(line):
    return line.rstrip("\r\n")


def _fence_start(line):
    match = re.match(r"^ {0,3}(`{3,})", _line_body(line))
    return match.group(1) if match else None


def _is_heading(line):
    return re.match(r"^ {0,3}#{1,6}(?:[ \t]+|$)", _line_body(line)) is not None


def _is_list_item(line):
    return re.match(
        r"^ {0,3}(?:[-+*][ \t]+|\d{1,9}[.)][ \t]+)",
        _line_body(line),
    ) is not None


def split_markdown_blocks(content):
    if not isinstance(content, str):
        raise ValueError("Nội dung markdown phải là văn bản.")
    lines = []
    offset = 0
    for line in content.splitlines(keepends=True):
        lines.append((line, offset, offset + len(line)))
        offset += len(line)
    if not lines:
        return []

    raw_blocks = []
    index = 0
    while index < len(lines):
        line, start, end = lines[index]
        body = _line_body(line)
        if not body.strip():
            index += 1
            continue

        fence = _fence_start(line)
        if fence:
            marker = fence[0]
            marker_length = len(fence)
            index += 1
            while index < len(lines):
                closing = re.match(
                    rf"^ {{0,3}}{re.escape(marker)}{{{marker_length},}}[ \t]*$",
                    _line_body(lines[index][0]),
                )
                end = lines[index][2]
                index += 1
                if closing:
                    break
            raw_blocks.append(("code", start, end))
            continue

        if _is_heading(line):
            raw_blocks.append(("heading", start, end))
            index += 1
            continue

        if _is_list_item(line):
            index += 1
            while index < len(lines):
                next_line = lines[index][0]
                next_body = _line_body(next_line)
                if not next_body.strip():
                    next_index = index
                    while (
                        next_index < len(lines)
                        and not _line_body(lines[next_index][0]).strip()
                    ):
                        next_index += 1
                    if (
                        next_index < len(lines)
                        and _is_list_item(lines[next_index][0])
                    ):
                        index = next_index
                        continue
                    break
                if _fence_start(next_line) or _is_heading(next_line):
                    break
                index += 1
            end = lines[index - 1][2]
            raw_blocks.append(("list", start, end))
            continue

        index += 1
        while index < len(lines):
            next_line = lines[index][0]
            if (
                not _line_body(next_line).strip()
                or _fence_start(next_line)
                or _is_heading(next_line)
                or _is_list_item(next_line)
            ):
                break
            index += 1
        end = lines[index - 1][2]
        raw_blocks.append(("paragraph", start, end))

    if not raw_blocks:
        return [{
            "type": "paragraph",
            "start": 0,
            "end": len(content),
            "content": content,
        }]

    blocks = []
    for index, (block_type, raw_start, _raw_end) in enumerate(raw_blocks):
        start = 0 if index == 0 else raw_start
        end = (
            raw_blocks[index + 1][1]
            if index + 1 < len(raw_blocks)
            else len(content)
        )
        blocks.append({
            "type": block_type,
            "start": start,
            "end": end,
            "content": content[start:end],
        })
    return blocks


def _project_name(project=None):
    if project is None:
        try:
            project = CURRENT_PROJECT_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            project = "default"
        if not is_valid_project_name(project):
            project = "default"
    elif not is_valid_project_name(project):
        raise ValueError("Tên project không hợp lệ.")
    return project


def _artifact_dir(project=None):
    project_dir = PROJECTS_DIR / _project_name(project)
    if project_dir.is_symlink():
        raise ValueError("Thư mục project không hợp lệ.")
    artifact_dir = project_dir / "artifacts"
    if artifact_dir.is_symlink():
        raise ValueError("Thư mục artifact không hợp lệ.")
    return artifact_dir


def _artifact_path(artifact_id, project=None):
    if (
        not isinstance(artifact_id, str)
        or ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None
    ):
        return None
    return _artifact_dir(project) / f"{artifact_id}.json"


def _validate_title(title):
    if not isinstance(title, str):
        raise ValueError("Tiêu đề artifact phải là văn bản.")
    title = title.strip()
    if not title or len(title) > MAX_TITLE_LENGTH:
        raise ValueError(
            f"Tiêu đề artifact phải dài từ 1 đến {MAX_TITLE_LENGTH} ký tự."
        )
    return title


def _validate_content(content):
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Nội dung artifact không được để trống.")
    if len(content) > MAX_CONTENT_LENGTH:
        raise ValueError(
            f"Nội dung artifact vượt quá {MAX_CONTENT_LENGTH} ký tự."
        )
    return content


def _validate_type_and_language(artifact_type, language):
    if artifact_type not in ("markdown", "code"):
        raise ValueError("Loại artifact phải là 'markdown' hoặc 'code'.")
    if artifact_type == "markdown":
        if language is not None:
            raise ValueError("Artifact markdown không được có language.")
        return {}
    if language is None:
        return {}
    if not isinstance(language, str) or not language.strip():
        raise ValueError("Language của artifact code không hợp lệ.")
    language = language.strip()
    if len(language) > MAX_LANGUAGE_LENGTH:
        raise ValueError(
            f"Language không được dài quá {MAX_LANGUAGE_LENGTH} ký tự."
        )
    return {"language": language}


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _validate_artifact(artifact, artifact_id):
    if (
        not isinstance(artifact, dict)
        or artifact.get("id") != artifact_id
        or not isinstance(artifact.get("title"), str)
        or artifact.get("type") not in ("markdown", "code")
        or not isinstance(artifact.get("content"), str)
        or not isinstance(artifact.get("versions"), list)
        or not isinstance(artifact.get("created"), str)
        or not isinstance(artifact.get("updated"), str)
    ):
        raise ValueError(f"Định dạng artifact không hợp lệ: {artifact_id}")
    if artifact["type"] == "code":
        if "language" in artifact and not isinstance(artifact["language"], str):
            raise ValueError(f"Language artifact không hợp lệ: {artifact_id}")
    elif "language" in artifact:
        raise ValueError(f"Artifact markdown không được có language: {artifact_id}")
    if not 1 <= len(artifact["versions"]) <= MAX_VERSIONS:
        raise ValueError(f"Lịch sử version artifact không hợp lệ: {artifact_id}")
    for version in artifact["versions"]:
        if (
            not isinstance(version, dict)
            or type(version.get("number")) is not int
            or version["number"] < 1
            or not isinstance(version.get("content"), str)
            or not isinstance(version.get("created"), str)
            or version.get("source") not in ("bot", "user")
        ):
            raise ValueError(f"Lịch sử version artifact không hợp lệ: {artifact_id}")
    if artifact["content"] != artifact["versions"][-1]["content"]:
        raise ValueError(f"Nội dung artifact không khớp version mới nhất: {artifact_id}")
    return artifact


def create_artifact(title, type, content, language=None, project=None):
    title = _validate_title(title)
    content = _validate_content(content)
    language_fields = _validate_type_and_language(type, language)
    artifact_id = uuid.uuid4().hex
    created = _timestamp()
    artifact = {
        "id": artifact_id,
        "title": title,
        "type": type,
        **language_fields,
        "content": content,
        "versions": [{
            "number": 1,
            "content": content,
            "created": created,
            "source": "bot",
        }],
        "created": created,
        "updated": created,
    }
    path = _artifact_path(artifact_id, project)
    path.parent.mkdir(parents=True, exist_ok=True)

    def create_if_missing(existing):
        if existing is not None:
            raise FileExistsError(f"Artifact đã tồn tại: {artifact_id}")
        return artifact

    update_json(path, None, create_if_missing)
    return artifact


def get_artifact(artifact_id, project=None):
    path = _artifact_path(artifact_id, project)
    if path is None or path.is_symlink() or not path.is_file():
        raise FileNotFoundError("Không tìm thấy artifact.")
    artifact = read_json(path, None)
    if artifact is None:
        raise FileNotFoundError("Không tìm thấy artifact.")
    return _validate_artifact(artifact, artifact_id)


def list_artifacts(project=None):
    artifact_dir = _artifact_dir(project)
    if not artifact_dir.exists():
        return []
    if not artifact_dir.is_dir():
        raise ValueError("Đường dẫn artifact không phải thư mục.")
    artifacts = []
    for path in artifact_dir.glob("*.json"):
        if (
            ARTIFACT_ID_PATTERN.fullmatch(path.stem) is None
            or path.is_symlink()
            or not path.is_file()
        ):
            continue
        artifact = get_artifact(path.stem, project)
        summary = {
            "id": artifact["id"],
            "title": artifact["title"],
            "type": artifact["type"],
            "created": artifact["created"],
            "updated": artifact["updated"],
            "version_count": len(artifact["versions"]),
        }
        if "language" in artifact:
            summary["language"] = artifact["language"]
        artifacts.append(summary)
    return sorted(
        artifacts,
        key=lambda item: (item["updated"], item["id"]),
        reverse=True,
    )


def update_artifact(
    artifact_id,
    content,
    project=None,
    source="user",
):
    content = _validate_content(content)
    if source not in ("bot", "user"):
        raise ValueError("Nguồn version phải là 'bot' hoặc 'user'.")
    path = _artifact_path(artifact_id, project)
    if path is None or path.is_symlink() or not path.is_file():
        raise FileNotFoundError("Không tìm thấy artifact.")
    updated = _timestamp()

    def append_version(artifact):
        if artifact is None:
            raise FileNotFoundError("Không tìm thấy artifact.")
        _validate_artifact(artifact, artifact_id)
        versions = artifact["versions"]
        number = versions[-1]["number"] + 1
        versions.append({
            "number": number,
            "content": content,
            "created": updated,
            "source": source,
        })
        artifact["versions"] = versions[-MAX_VERSIONS:]
        artifact["content"] = content
        artifact["updated"] = updated
        return artifact

    return update_json(path, None, append_version)


def apply_patch(
    artifact_id,
    base_version,
    start,
    end,
    new_text,
    source,
    project=None,
):
    if type(base_version) is not int or base_version < 1:
        raise ValueError("base_version phải là số nguyên dương.")
    if type(start) is not int or type(end) is not int:
        raise ValueError("Vị trí start và end phải là số nguyên.")
    if not isinstance(new_text, str):
        raise ValueError("Nội dung thay thế phải là văn bản.")
    if source not in ("bot", "user"):
        raise ValueError("Nguồn version phải là 'bot' hoặc 'user'.")

    path = _artifact_path(artifact_id, project)
    if path is None or path.is_symlink() or not path.is_file():
        raise FileNotFoundError("Không tìm thấy artifact.")
    updated = _timestamp()

    def replace_range(artifact):
        if artifact is None:
            raise FileNotFoundError("Không tìm thấy artifact.")
        _validate_artifact(artifact, artifact_id)
        latest_version = artifact["versions"][-1]["number"]
        if base_version != latest_version:
            raise ArtifactVersionConflict(
                f"Artifact đã đổi sang version {latest_version}; "
                f"base_version {base_version} đã cũ. Hãy tải lại artifact."
            )
        content = artifact["content"]
        if start < 0 or end < start or end > len(content):
            raise ValueError(
                f"Vùng chọn [{start}, {end}) nằm ngoài nội dung "
                f"dài {len(content)} ký tự."
            )
        patched_content = _validate_content(
            content[:start] + new_text + content[end:]
        )
        versions = artifact["versions"]
        versions.append({
            "number": latest_version + 1,
            "content": patched_content,
            "created": updated,
            "source": source,
        })
        artifact["versions"] = versions[-MAX_VERSIONS:]
        artifact["content"] = patched_content
        artifact["updated"] = updated
        return artifact

    return update_json(path, None, replace_range)
