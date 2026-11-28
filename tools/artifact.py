import json
import threading
from typing import Optional

from artifacts import create_artifact as save_artifact


_ARTIFACT_CONTEXT = threading.local()


def set_artifact_handler(handler):
    previous = getattr(_ARTIFACT_CONTEXT, "handler", None)
    if handler is None:
        if hasattr(_ARTIFACT_CONTEXT, "handler"):
            del _ARTIFACT_CONTEXT.handler
    else:
        _ARTIFACT_CONTEXT.handler = handler
    return previous


def create_artifact(
    title: str,
    type: str,
    content: str,
    language: Optional[str] = None,
) -> str:
    """Create a long, editable Markdown document or code artifact. Do NOT use for short answers; answer those directly. type must be markdown or code; provide language for code when known."""
    artifact = save_artifact(title, type, content, language)
    info = {
        "id": artifact["id"],
        "title": artifact["title"],
        "artifact_type": artifact["type"],
    }
    handler = getattr(_ARTIFACT_CONTEXT, "handler", None)
    if handler is not None:
        handler(info)
    return json.dumps(info, ensure_ascii=False)
