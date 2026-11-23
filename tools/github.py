import json
import os
import urllib.error
import urllib.request

API = "https://api.github.com"
BO_QUA = ("node_modules/", ".venv/", "/bin/", "/obj/", "__pycache__/", ".git/")


def _get(url: str):
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "chatbot-ca-nhan"},
    )
    token = os.getenv("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def read_github(repo: str) -> str:
    """Read a GitHub repository: description, file list and the 15 latest commits.
    repo is "owner/name" or a github.com URL."""
    print(f"  [tool] read_github {repo}")
    repo = repo.strip().removeprefix("https://github.com/").removesuffix(".git")
    repo = "/".join(repo.strip("/").split("/")[:2])
    try:
        info = _get(f"{API}/repos/{repo}")
        nhanh = info["default_branch"]
        cay = _get(f"{API}/repos/{repo}/git/trees/{nhanh}?recursive=1")
        commits = _get(f"{API}/repos/{repo}/commits?per_page=15")
    except urllib.error.HTTPError as e:
        return (f"Lỗi GitHub {e.code}: repo không tồn tại, là repo riêng tư "
                "(cần GITHUB_TOKEN trong .env) hoặc đã hết hạn mức.")
    except Exception as e:
        return f"Lỗi khi gọi GitHub: {e}"

    dong = [f"Repo: {repo}", f"Mô tả: {info.get('description') or '(không có)'}",
            f"Nhánh mặc định: {nhanh}", "", "File:"]
    files = [n["path"] for n in cay.get("tree", [])
             if n["type"] == "blob" and not any(b in "/" + n["path"] for b in BO_QUA)]
    dong += [f"- {p}" for p in files[:150]]
    if len(files) > 150:
        dong.append(f"... và {len(files) - 150} file nữa")
    dong += ["", "Commit gần nhất:"]
    for c in commits:
        tac_gia = c["commit"]["author"]["name"]
        ngay = c["commit"]["author"]["date"][:10]
        msg = c["commit"]["message"].splitlines()[0]
        dong.append(f"- {c['sha'][:7]} | {ngay} | {tac_gia} | {msg}")
    return "\n".join(dong)