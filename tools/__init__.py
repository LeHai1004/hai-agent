from .web import web_search
from .time_tool import get_current_time
from .files import list_files, read_file, write_file
from .word import create_word
from .repo import read_repo, draw_diagram
from .github import read_github, github_stats, github_commit, github_branches
import functools

TOOLS = [web_search, get_current_time, list_files, read_file, write_file, create_word,
         read_repo, draw_diagram, read_github, github_stats, github_commit, github_branches]


# tool nặng: (lý do, gợi ý cách hỏi nhẹ hơn)
NANG = {
    "github_branches": (
        "gọi GitHub khoảng 28 lần (hạn mức chỉ 60 lần/giờ khi chưa có token)",
        "chỉ hỏi khi thật cần xem nhánh, đừng chạy lặp lại",
    ),
    "github_commit": (
        "có thể trả về nội dung code dài (ước lượng tới ~4.000 token nếu kèm patch)",
        "chỉ định rõ 1 file cần xem thay vì cả commit",
    ),
    "read_repo": (
        "đưa cả cấu trúc dự án cho AI (ước lượng tới ~2.700 token)",
        "hỏi về một file/thư mục cụ thể nếu chỉ cần chỗ đó",
    ),
}


def _hoi_truoc(fn, ly_do, goi_y):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        print(f"\n⚠ Tool '{fn.__name__}' {ly_do}.")
        print(f"  Gợi ý: {goi_y}.")
        if input("  Vẫn chạy? (y/n): ").strip().lower() != "y":
            return ("Người dùng đã từ chối chạy tool này. Không gọi lại tool nào, "
                    "chỉ trả lời ngắn gọn rằng đã hủy.")
        return fn(*args, **kwargs)
    return wrapper


TOOLS = [_hoi_truoc(f, *NANG[f.__name__]) if f.__name__ in NANG else f for f in TOOLS]