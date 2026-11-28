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
    """Create an editable document, guide, or code/function for the user to read, copy, or revise. Use for writing requests even if the user does not say 'artifact'. Do not use for short answers or ordinary explanations; reply in chat. Use write_file only when the user explicitly asks to save content in the workspace or project directory. type must be markdown or code; provide language for code when known."""
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
