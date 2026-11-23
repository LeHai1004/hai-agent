from llm import ask
from tools.github import github_stats, github_branches, read_github


def build_git_prompt(repo, stats_text, branches_text, commits_text):
    return (
        f"Bạn là Git Coach, chuyên gia phân tích kho lưu trữ Git/GitHub.\n"
        f"Dựa trên dữ liệu thực tế dưới đây của repository '{repo}', hãy giải thích bằng tiếng Việt thật ngắn gọn, súc tích, rõ ràng theo đúng 3 ý sau:\n"
        f"(1) Ai làm bao nhiêu commit (dựa vào số liệu thống kê tác giả).\n"
        f"(2) Nhánh nào đang hoạt động (dựa vào danh sách nhánh).\n"
        f"(3) Cách đọc một dòng commit (giải thích cấu trúc hash, message, tác giả, ngày từ vài commit gần nhất).\n\n"
        f"Chỉ dùng số liệu từ kết quả tool dưới đây, không bịa số.\n\n"
        f"=== THỐNG KÊ TÁC GIẢ ===\n{stats_text}\n\n"
        f"=== DANH SÁCH NHÁNH ===\n{branches_text}\n\n"
        f"=== COMMIT GẦN NHẤT ===\n{commits_text}\n"
    )


def git_coach(gemini_client, openai_clients, repo_input):
    repo = repo_input.strip()
    if not repo:
        return "Vui lòng nhập repo theo cú pháp: /git owner/repo", None, None

    print(f"\nĐang thu thập thông tin git cho '{repo}'...")
    
    # Gọi trực tiếp các hàm trong tools/github.py
    stats_res = github_stats(repo)
    branches_res = github_branches(repo)
    
    # Lấy vài commit gần nhất bằng cách gọi read_github hoặc API lấy commits
    # read_github đã bao gồm mô tả, file list và 15 commit gần nhất
    github_info = read_github(repo)
    
    # In kết quả thô ra terminal trước
    print("\n--- KẾT QUẢ THÔ TỪ GITHUB ---")
    print("1. Thống kê:")
    print(stats_res)
    print("\n2. Nhánh:")
    print(branches_res)
    print("\n3. Thông tin repo & commit gần nhất:")
    print(github_info)
    print("------------------------------\n")

    prompt = build_git_prompt(repo, stats_res, branches_res, github_info)
    
    # Gọi model để giải thích bằng tiếng Việt
    return ask(gemini_client, openai_clients, [], prompt, use_history=False, route_text=repo)
