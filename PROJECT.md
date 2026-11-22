# AI Chatbot cá nhân

## Mục tiêu

Tự xây chatbot/agent riêng, dùng hằng ngày, không lo hết lượt.

## Hướng đi

- Lõi viết bằng Python
- Mượn model và công cụ có sẵn qua chuẩn mở (API, MCP)

## Tính năng dự kiến (làm dần)

- Chat + gọi tool/skill
- Tự chọn model theo câu hỏi, tự chuyển nhà cung cấp khi hết lượt
- Import/export lịch sử chat không mất chi tiết
- Không bịa, có nguồn xác minh

## Quy tắc làm việc

- Làm từng bước nhỏ, trả lời ngắn gọn
- Thiết kế dễ mở rộng, không đập đi xây lại

## Trạng thái hiện tại (22/11/2026)

- Python 3.12 trong .venv; chạy: python chat.py
- Cấu trúc: chat.py (vòng lặp lệnh), config.py, history.py, usage.py, search.py, llm.py, tools/ (web.py, time_tool.py, **init**.py chứa TOOLS)
- Đã có: chat terminal Gemini, lưu/nạp lịch sử (history/), tự chuyển model, đếm lượt/token (/usage), /web tìm DuckDuckGo có nguồn
- Bot tự gọi tool (function calling): web_search, get_current_time; client dùng API v1beta
- Thêm tool mới: tạo hàm Python có type hints + docstring trong tools/, thêm vào TOOLS
- Lỗi đã gặp: Google Search tool báo 429/400 trên key miễn phí nên dùng DuckDuckGo; gemini-2.5-flash-lite đã bị gỡ
- Việc tiếp theo: tool đọc/ghi file trong thư mục dự án (Cline làm), sau đó tạo file Word, import/export lịch sử
