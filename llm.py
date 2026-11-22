from google.genai import types
from config import MODELS


def to_contents(messages):
    return [
        types.Content(role=m["role"], parts=[types.Part(text=m["text"])])
        for m in messages
    ]


def ask(client, messages, text, use_history=True):
    history = to_contents(messages) if use_history else []
    last_error = None
    for model in MODELS:
        try:
            chat = client.chats.create(model=model, history=history)
            reply = chat.send_message(text)
            return reply.text, model, reply.usage_metadata
        except Exception as e:
            last_error = e
            print(f"  [{model}] lỗi, thử model khác: {str(e)[:120]}")
    raise last_error
