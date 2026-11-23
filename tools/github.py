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

def _chuan_hoa(repo: str) -> str:
    repo = repo.strip().removeprefix("https://github.com/").removesuffix(".git")
    return "/".join(repo.strip("/").split("/")[:2])


def github_stats(repo: str, branch: str = "") -> str:
    """Count commits per author over the whole history of a GitHub repository.
    repo is "owner/name" or a github.com URL. branch is optional (default branch if empty)."""
    print(f"  [tool] github_stats {repo} {branch}")
    repo = _chuan_hoa(repo)
    dem = {}
    tong = 0
    try:
        for trang in range(1, 11):  # tối đa 1000 commit
            url = f"{API}/repos/{repo}/commits?per_page=100&page={trang}"
            if branch:
                url += f"&sha={branch}"
            ds = _get(url)
            for c in ds:
                ten = (c.get("author") or {}).get("login") or c["commit"]["author"]["name"]
                ngay = c["commit"]["author"]["date"][:10]
                d = dem.setdefault(ten, {"so": 0, "dau": ngay, "cuoi": ngay})
                d["so"] += 1
                d["dau"] = min(d["dau"], ngay)
                d["cuoi"] = max(d["cuoi"], ngay)
                tong += 1
            if len(ds) < 100:
                break
    except urllib.error.HTTPError as e:
        return f"Lỗi GitHub {e.code}: repo/nhánh không tồn tại, là repo riêng tư hoặc hết hạn mức."
    except Exception as e:
        return f"Lỗi khi gọi GitHub: {e}"

    dong = [f"Repo: {repo} | nhánh: {branch or 'mặc định'} | tổng {tong} commit", ""]
    for ten, d in sorted(dem.items(), key=lambda x: -x[1]["so"]):
        phan_tram = d["so"] * 100 // tong if tong else 0
        dong.append(f"- {ten}: {d['so']} commit ({phan_tram}%), từ {d['dau']} đến {d['cuoi']}")
    kq = "\n".join(dong)
    print(kq)
    return kq

def github_commit(repo: str, sha: str, file: str = "") -> str:
    """Show the details of one commit in a GitHub repository: author, date, message
    and the files it changed with lines added and removed. sha can be the short 7-char form.
    If file is given (part of a file name), also include the actual code changes (patch)
    of the matching files."""
    print(f"  [tool] github_commit {repo} {sha} {file}")
    repo = _chuan_hoa(repo)
    try:
        c = _get(f"{API}/repos/{repo}/commits/{sha.strip()}")
    except urllib.error.HTTPError as e:
        return f"Lỗi GitHub {e.code}: repo/commit không tồn tại, là repo riêng tư hoặc hết hạn mức."
    except Exception as e:
        return f"Lỗi khi gọi GitHub: {e}"

    info = c["commit"]
    tk = c.get("stats", {})
    files = c.get("files", [])
    dong = [
        f"Commit {c['sha'][:7]} | {info['author']['name']} | {info['author']['date'][:10]}",
        f"Nội dung: {info['message']}",
        f"Tổng: +{tk.get('additions', 0)} -{tk.get('deletions', 0)} dòng, {len(files)} file",
        "",
        "File đã đổi:",
    ]
    for f in files[:50]:
        dong.append(f"- {f['filename']} ({f['status']}, +{f['additions']} -{f['deletions']})")
    if len(files) > 50:
        dong.append(f"... và {len(files) - 50} file nữa")

    if file:
        khop = [f for f in files if file.lower() in f["filename"].lower()]
        if not khop:
            dong.append(f"\nKhông có file nào khớp '{file}' trong commit này.")
        for f in khop[:3]:
            patch = f.get("patch", "(không có nội dung, có thể là file nhị phân hoặc quá lớn)")
            if len(patch) > 4000:
                patch = patch[:4000] + "\n... (đã cắt bớt)"
            dong += ["", f"=== Thay đổi của {f['filename']} ===", patch]

    kq = "\n".join(dong)
    print(kq)
    return kq