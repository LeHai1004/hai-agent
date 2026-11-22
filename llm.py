from google.genai import types
from config import MODELS
from tools import TOOLS

SYSTEM_INSTRUCTION = (
    "Trả lời bằng tiếng Việt, ngắn gọn. "
    "Khi dùng thông tin từ web_search phải ghi số nguồn [n] và liệt kê link; "
    "không biết thì nói không biết, không bịa."
)


def to_contents(messages):
    return [
        types.Content(role=m["role"], parts=[types.Part(text=m["text"])])
        for m in messages
    ]


def ask(client, messages, text, use_history=True):
    history = to_contents(messages) if use_history else []
    config = types.GenerateContentConfig(
        tools=TOOLS,
        system_instruction=SYSTEM_INSTRUCTION,
    )
    last_error = None
    for model in MODELS:
        try:
            chat = client.chats.create(model=model, history=history, config=config)
            reply = chat.send_message(text)
            return reply.text, model, reply.usage_metadata
        except Exception as e:
            last_error = e
            print(f"  [{model}] lỗi, thử model khác: {str(e)[:120]}")
    raise last_error

