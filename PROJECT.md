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

## Trạng thái hiện tại (cập nhật 23/11/2026)

**Chạy:** `python chat.py` trong .venv (Python 3.12). Key trong .env: GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY (tùy chọn GITHUB_TOKEN cho repo riêng tư).

**Lệnh trong chat:** /new, /usage, /web câu hỏi, /en câu, /git owner/repo, /vis nội dung, /export, /import, exit.

**File chính:** chat.py (vòng lặp chat, cảnh báo lịch sử dài NGUONG_CANH_BAO=8000), llm.py (gọi model, chạy tool, chọn model theo câu hỏi bằng HARD_WORDS, SYSTEM_INSTRUCTION), config.py, history.py, usage.py, search.py (/web bằngDuckDuckGo), transfer.py (export/import), translate.py (/en), gitcoach.py (/git), visual.py (/vis - xuất cả vis.md và vis.html hoạt hình).

**Tool (thư mục tools/, đăng ký trong tools/**init**.py):** web_search, get_current_time, list_files/read_file/write_file (thư mục workspace/, ghi file hỏi y/n), create_word, read_repo (quét cấu trúc dự án), draw_diagram (sơ đồ Mermaid lưu workspace/diagram.md), read_github, github_stats, github_commit (có tham số file để xem patch), github_branches, read_webpage (đọc 1 trang web, chặn địa chỉ nội bộ, dùng certifi).

**Tool nặng (hỏi y/n trước khi chạy):** github_branches, github_commit, read_repo, khai báo trong dict NANG ở tools/**init**.py.

**Quy ước làm việc:** làm từng bước nhỏ, commit sau mỗi bước; tool lỗi phải báo rõ, không bịa; kiểm tra tool bằng python -c trước rồi mới thử qua bot; tool mới viết trong tools/ rồi thêm vào TOOLS.

**Hạn chế đã biết:** gemini-3.5-flash chỉ ~20 lượt/ngày (hết thì tự chuyển gemini-2.5-flash); model lite tóm tắt kết quả web đôi khi sai; GitHub chưa có token chỉ 60 lần gọi/giờ; read_webpage chỉ lấy ~6000 ký tự và không chạy JavaScript.

**Việc tiếp theo (chưa làm):** không gian "project" riêng; ghi chú "chưa xác nhận" khi tóm tắt web.

- Chọn model (llm.py): score_question chấm điểm câu hỏi (độ dài, code/traceback, từ khóa mạnh/vừa khớp theo từ nguyên). Điểm >= 3 thì thử model mạnh trước, thấp hơn thì thử flash-lite trước.
- available_models bỏ qua model đã dùng hết LIMITS trong ngày (đọc history/usage.json); nếu tất cả đều hết thì vẫn thử lại theo thứ tự cũ.
- SYSTEM_INSTRUCTION: câu kiến thức ổn định thì không gọi web_search.
- Kiểm tra điểm: python test_route.py
