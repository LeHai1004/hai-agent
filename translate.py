from llm import ask


def build_en_prompt(text):
    return (
        "Translate between Vietnamese and English. "
        "If the input is Vietnamese, translate to English; "
        "if it is English, translate to Vietnamese. "
        "Output exactly this format, nothing else:\n"
        "VI: <câu tiếng Việt>\n"
        "EN: <English sentence>\n"
        "Từ vựng: <3-5 từ hoặc cụm quan trọng, dạng english = nghĩa, cách nhau bằng dấu ;>\n\n"
        f"Input: {text}"
    )


def translate(gemini_client, openai_clients, text):
        return ask(gemini_client, openai_clients, [], build_en_prompt(text), use_history=False, route_text=text)