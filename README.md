# hai-agent

Chatbot/agent chạy trong terminal, tự xây bằng Python. Không phụ thuộc một nhà cung cấp: tự chọn model Gemini, Groq hoặc OpenRouter theo độ khó câu hỏi, tự chuyển model khác khi lỗi hoặc hết lượt.

## Điểm nổi bật

- **Router nhiều model:** câu ngắn đi model nhẹ (tiết kiệm lượt), câu khó đi model mạnh, lỗi thì tự thử model kế tiếp.
- **Tool calling:** tìm web, đọc 1 trang web, đọc/ghi file trong workspace của project (ghi file luôn hỏi y/n), tạo file Word, đọc repo GitHub.
- **Lệnh riêng:** dịch song ngữ `/en`, phân tích repo `/git`, giải thích trực quan có hoạt hình `/vis`.
- **Lịch sử và lượt dùng:** lưu hội thoại, xem lượt dùng, export/import lịch sử.
- **Bộ nhớ toàn cục:** sau mỗi câu trả lời, trích xuất ngầm thông tin phù hợp do người dùng nói; hỗ trợ yêu cầu nhớ/quên bằng lời tự nhiên và dùng chung giữa mọi project.

## Lệnh

| Lệnh              | Tác dụng                                                                               |
| ----------------- | -------------------------------------------------------------------------------------- |
| `/new`            | Bắt đầu cuộc chat mới                                                                  |
| `/nho nội dung`   | Ghi nhớ thông tin trên mọi project                                                     |
| `/bonho`          | Liệt kê bộ nhớ toàn cục                                                                |
| `/quen số`        | Xóa mục bộ nhớ theo số thứ tự                                                          |
| `/usage`          | Xem lượt dùng các model                                                                |
| `/web câu hỏi`    | Tìm web (DuckDuckGo) rồi trả lời kèm nguồn                                             |
| `/en câu`         | Tự nhận diện chiều Việt-Anh/Anh-Việt, kèm vài từ vựng chính                            |
| `/git owner/repo` | Git Coach: ai làm bao nhiêu commit, nhánh nào đang hoạt động, cách đọc một dòng commit |
| `/vis nội dung`   | Giải thích quy trình/code bằng bức tranh toàn cảnh, sơ đồ Mermaid và hoạt hình         |
| `exit`            | Thoát                                                                                  |

## Demo `/vis`

```
/vis Khi bấm Run một chương trình Java thì code chạy từ RAM hay gửi request lên server rồi quay về?
```

Bot trả đủ 4 phần: bức tranh toàn cảnh (cả những bước bạn chưa hỏi), sơ đồ Mermaid lưu trong workspace của project hiện tại, các bước đánh số ghi rõ dữ liệu đang nằm ở RAM, ổ đĩa hay mạng, và 3 câu nên hỏi tiếp. Đồng thời mở trang `vis.html` có nút Trước / Sau / Tự chạy để xem chấm sáng chạy giữa các thành phần.

## Cài đặt

```bash
git clone https://github.com/LeHai1004/hai-agent.git
cd hai-agent
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Tạo file `.env` (đã nằm trong `.gitignore`, không bị đưa lên GitHub), cần ít nhất một khóa:

```
GEMINI_API_KEY=your_key
GROQ_API_KEY=your_key
OPENROUTER_API_KEY=your_key
GITHUB_TOKEN=your_token
```

`GITHUB_TOKEN` là tùy chọn, không có thì GitHub chỉ cho khoảng 60 lần gọi mỗi giờ.

Chạy:

```bash
python chat.py
```

## Cấu trúc

```
chat.py        vòng lặp chat và xử lý lệnh
llm.py         gọi model, router, tool calling
config.py      danh sách model và nhà cung cấp
history.py     lưu và nạp lịch sử
usage.py       đếm lượt dùng
search.py      /web
transfer.py    export/import lịch sử
translate.py   /en
gitcoach.py    /git
visual.py      /vis
tools/         các tool bot được phép gọi
workspace/     workspace của project mặc định; project khác dùng history/projects/<tên>/workspace/
PROJECT.md     ghi chú trạng thái cho agent code
```

## Hạn chế đã biết

- Gói miễn phí giới hạn lượt, model mạnh chỉ khoảng 20 lượt mỗi ngày, hết thì tự chuyển model khác.
- Model nhẹ tóm tắt kết quả web đôi khi sai chi tiết; kiến thức quan trọng nên đối chiếu nguồn.
- `read_webpage` chỉ lấy khoảng 6000 ký tự đầu và không chạy JavaScript.

## Hướng phát triển

- Chọn model theo loại câu hỏi tốt hơn.
- Mở rộng thêm các công cụ quản lý project.
- Ghi chú "chưa xác nhận" khi tóm tắt nội dung web.
