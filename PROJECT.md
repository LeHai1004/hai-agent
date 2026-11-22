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
- Đã có: chat terminal Gemini, lưu/nạp lịch sử (history/), tự chuyển model,
  đếm lượt/token (/usage), tìm web DuckDuckGo (/web, có nguồn, tự thử lại khi timeout)
- /web không gửi lịch sử cũ, nguồn được lưu kèm câu trả lời
- Lỗi đã gặp: Google Search tool báo 429/400 trên key miễn phí nên dùng DuckDuckGo;
  gemini-2.5-flash-lite đã bị gỡ, thay bằng gemini-3.5-flash-lite
- Việc tiếp theo: tách chat.py thành nhiều file (module), dùng Cline
