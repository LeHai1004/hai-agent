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

**Lệnh trong chat:** /new, /usage, /web câu hỏi, /en câu, /git owner/repo, /vis nội dung, /nho nội dung, /bonho, /quen số, /export, /import, exit. Bộ nhớ toàn cục lưu ở history/memory/global.json; hệ thống tự trích xuất thông tin phù hợp sau mỗi câu trả lời, hỗ trợ yêu cầu nhớ/quên bằng lời tự nhiên và có nhánh dự phòng khi rời chat.

**File chính:** chat.py (vòng lặp chat, cảnh báo lịch sử dài NGUONG_CANH_BAO=8000), llm.py (gọi model, chạy tool, chọn model theo câu hỏi bằng HARD_WORDS, SYSTEM_INSTRUCTION), memory.py (bộ nhớ toàn cục), config.py, history.py, usage.py, search.py (/web bằngDuckDuckGo), transfer.py (export/import), translate.py (/en), gitcoach.py (/git), visual.py (/vis - xuất cả vis.md và vis.html hoạt hình).

**Workspace:** project `default` dùng `workspace/` để giữ tương thích; project khác dùng `history/projects/<tên>/workspace/`.

**Tool (thư mục tools/, đăng ký trong tools/**init**.py):** web_search, get_current_time, list_files/read_file/write_file (workspace của project hiện tại, ghi file hỏi y/n), create_word, read_repo (quét cấu trúc dự án), draw_diagram (sơ đồ Mermaid lưu trong workspace của project hiện tại), read_github, github_stats, github_commit (có tham số file để xem patch), github_branches, read_webpage (đọc 1 trang web, chặn địa chỉ nội bộ, dùng certifi).

**Tool nặng (hỏi y/n trước khi chạy):** github_branches, github_commit, read_repo, khai báo trong dict NANG ở tools/**init**.py.

**Quy ước làm việc:** làm từng bước nhỏ, commit sau mỗi bước; tool lỗi phải báo rõ, không bịa; kiểm tra tool bằng python -c trước rồi mới thử qua bot; tool mới viết trong tools/ rồi thêm vào TOOLS.

**Hạn chế đã biết:** gemini-3.5-flash chỉ ~20 lượt/ngày (hết thì tự chuyển gemini-2.5-flash); model lite tóm tắt kết quả web đôi khi sai; GitHub chưa có token chỉ 60 lần gọi/giờ; read_webpage chỉ lấy ~6000 ký tự và không chạy JavaScript.

**Việc tiếp theo (chưa làm):** ghi chú "chưa xác nhận" khi tóm tắt web.

- Chọn model (llm.py): score_question chấm điểm câu hỏi (độ dài, code/traceback, từ khóa mạnh/vừa khớp theo từ nguyên). Điểm >= 3 thì thử model mạnh trước, thấp hơn thì thử flash-lite trước.
- available_models bỏ qua model đã dùng hết LIMITS trong ngày (đọc history/usage.json); nếu tất cả đều hết thì vẫn thử lại theo thứ tự cũ.
- SYSTEM_INSTRUCTION: câu kiến thức ổn định thì không gọi web_search.
- Kiểm tra điểm: python test_route.py
- /play nội dung: Gemini chia chủ đề thành 5-8 cảnh JSON (visual.py: build_scenes_prompt, parse_scenes, save_scenes_html, play), ghi vis_play.html trong workspace của project hiện tại từ template vis_player.html, mở bằng trình duyệt, có giọng đọc tiếng Việt.
- /en câu: tự nhận diện chiều dịch theo đoạn (tiếng Việt sang Anh, ngôn ngữ khác sang Việt), model trả JSON (câu + từ vựng), ghi bilingual.html trong workspace của project hiện tại (hai cột Bản gốc | Bản dịch, bấm câu để nghe bằng giọng Việt hoặc Anh) và mở trong trình duyệt; vẫn in bản text ra terminal.
- /pin nội dung, /pins, /unpin số: ghim tối đa 10 ghi chú quan trọng trong pins.json của project hiện tại, tự đưa vào system instruction ở mọi lần hỏi nên chat mới (/new) vẫn nhớ, không cần gửi lại lịch sử.
- /read đường_dẫn.pdf [số trang]: đọc PDF bằng pymupdf theo từng khối đoạn, tự chọn chiều dịch cho mỗi khối, ghi pdf_trangN.html trong workspace của project hiện tại, hai cột Bản gốc | Bản dịch; tiếng Việt và tiếng Anh dùng giọng đọc tương ứng. Đường dẫn kết thúc ở token đầu tiên có đuôi .pdf; số trang tùy chọn theo sau, phần chữ dư bị bỏ qua.
  Bản dịch từng trang được cache ở history/pdf_cache.json (khóa v2), mở lại không tốn lượt.
- /img đường*dẫn*ảnh câu hỏi: gửi ảnh (png, jpg, webp, tối đa 4 MB) cho Gemini hỏi đáp, ảnh không lưu vào lịch sử chat; chỉ chọn model Gemini vì Groq/OpenRouter chưa đọc ảnh.
- /file đường_dẫn câu hỏi: hỏi bot về file chữ hoặc PDF (cắt 24000 ký tự đầu), chặn .env/khóa bí mật, không lưu lịch sử.
