"""Chuẩn bị đầu vào cho nút "Kiểm tra code" trên trang web (app/demo.py).

Ba nguồn, mỗi nguồn trả về một `Prepared` để app.demo gọi `python -m app.cli --repo <...> --diff-file <...>`:
  - link GitHub công khai: repo, .../pull/<số>, .../tree/<nhánh> hoặc .../commit/<sha>. Tải về bằng git fetch
    độ sâu 1 (không bao giờ clone cả lịch sử), tự tính diff giữa nhánh gốc và nhánh cần xem;
  - diff dán vào (kết quả `git diff`), không cần repo: ngữ cảnh file để trống;
  - đoạn code dán vào: biến thành diff "file mới" để pipeline review như một file vừa thêm.

Code của người dùng KHÔNG BAO GIỜ được thực thi ở đây; git chạy với timeout và GIT_TERMINAL_PROMPT=0
(repo riêng tư trả lỗi ngay thay vì treo chờ mật khẩu). Lỗi do đầu vào ném InputError -> HTTP 422 / job failed
với thông báo dễ hiểu.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass

GITHUB_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?"
    r"(?:/(?:pull/(\d+)(?:/[^\s?#]*)?|tree/([^\s?#]+?)|commit/([0-9a-fA-F]{7,40})))?/?(?:[?#].*)?$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$")       # tên nhánh an toàn để đưa vào argv của git
FILENAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}$")    # tên file cho đoạn code dán vào (không có thư mục)
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"           # sha của cây rỗng: diff "từ không có gì"

GIT_TIMEOUT = 120          # giây cho mỗi lệnh git
MAX_DIFF_CHARS = 400_000   # thay đổi lớn hơn thì từ chối (token LLM và thời gian)
MAX_TEXT_CHARS = 200_000   # diff / code dán vào


class InputError(ValueError):
    """Lỗi do đầu vào của người dùng, thông báo viết cho người không chuyên."""


@dataclass
class Prepared:
    repo_path: str      # thư mục để app.cli đọc ngữ cảnh file (có thể rỗng với diff dán vào)
    base: str           # tên hiển thị của bên "trước"
    head: str           # tên hiển thị của bên "sau"
    diff_path: str      # file diff để đưa vào --diff-file
    label: str          # dòng mô tả cho trang web, ví dụ "octocat/hello PR #12"
    head_sha: str = ""
    title: str = ""


# ---------------------------------------------------------------- link GitHub

@dataclass
class GitHubTarget:
    owner: str
    repo: str
    pr: int | None = None
    branch: str | None = None
    commit: str | None = None

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}.git"

    @property
    def label(self) -> str:
        name = f"{self.owner}/{self.repo}"
        if self.pr:
            return f"{name} PR #{self.pr}"
        if self.branch:
            return f"{name} nhánh {self.branch}"
        if self.commit:
            return f"{name} commit {self.commit[:8]}"
        return f"{name} (commit mới nhất)"


def parse_github_url(url: str) -> GitHubTarget:
    m = GITHUB_RE.match((url or "").strip())
    if not m:
        raise InputError("Chỉ nhận link GitHub công khai dạng https://github.com/<tên>/<repo>, "
                         ".../pull/<số>, .../tree/<nhánh> hoặc .../commit/<mã>.")
    owner, repo, pr, branch, commit = m.groups()
    if branch and (not REF_RE.match(branch) or ".." in branch):
        raise InputError(f"Tên nhánh không hợp lệ: {branch!r}")
    return GitHubTarget(owner, repo, int(pr) if pr else None, branch, commit)


def _git(cwd: str, *args: str, timeout: int = GIT_TIMEOUT) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    try:
        p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise InputError(f"Lệnh git {args[0]} quá {timeout} giây: repo quá lớn hoặc mạng chậm. Hãy thử PR nhỏ hơn.")
    except OSError as e:
        raise InputError(f"Không chạy được git trên máy chủ: {e}")
    if p.returncode != 0:
        msg = (p.stderr or p.stdout).strip().splitlines()
        tail = msg[-1] if msg else "không rõ lý do"
        if "could not read Username" in tail or "Authentication failed" in tail or "Repository not found" in tail:
            raise InputError("Không tải được repo: link sai, repo riêng tư hoặc đã bị xoá. Chỉ repo công khai mới xem được.")
        if "couldn't find remote ref" in tail:
            raise InputError(f"Không tìm thấy nhánh/PR đó trên GitHub ({tail}).")
        raise InputError(f"git {args[0]} lỗi: {tail[:200]}")
    return p.stdout


def default_branch(repo_dir: str) -> str:
    """Nhánh mặc định của remote `origin` (main/master/...), hỏi bằng ls-remote nên không cần tải gì."""
    out = _git(repo_dir, "ls-remote", "--symref", "origin", "HEAD", timeout=60)
    for line in out.splitlines():                       # "ref: refs/heads/main\tHEAD"
        if line.startswith("ref: refs/heads/"):
            return line.split()[1][len("refs/heads/"):]
    raise InputError("Không xác định được nhánh mặc định của repo.")


def pr_info(t: GitHubTarget, timeout: int = 10) -> dict:
    """Nhánh gốc và tiêu đề của PR qua GitHub API (không cần token; 60 lượt/giờ). Lỗi thì trả {}."""
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{t.owner}/{t.repo}/pulls/{t.pr}",
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "HermesQA-demo"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except Exception:
        return {}
    base = (d.get("base") or {}).get("ref")
    return {"base": base if base and REF_RE.match(base) else None, "title": d.get("title") or ""}


def fetch_github(t: GitHubTarget, base: str, workdir: str, on_step=None) -> Prepared:
    """Tải phần cần thiết của repo về `workdir/src` (độ sâu 1) và ghi diff ra `workdir/input.diff`."""
    def step(detail: str) -> None:
        if on_step:
            on_step(detail)

    dest = os.path.join(workdir, "src")
    os.makedirs(dest, exist_ok=True)
    _git(dest, "init", "-q")
    _git(dest, "remote", "add", "origin", t.clone_url)

    title, base_sha = "", ""
    if t.pr:
        info = pr_info(t)
        title = info.get("title", "")
        base = base or info.get("base") or default_branch(dest)
        step(f"tải PR #{t.pr} của {t.owner}/{t.repo}")
        _git(dest, "fetch", "-q", "--depth", "1", "origin", f"pull/{t.pr}/head")
        head_sha = _git(dest, "rev-parse", "FETCH_HEAD").strip()
        head = f"PR #{t.pr}"
    elif t.commit:
        step(f"tải commit {t.commit[:8]} của {t.owner}/{t.repo}")
        _git(dest, "fetch", "-q", "--depth", "2", "origin", t.commit)
        head_sha = _git(dest, "rev-parse", "FETCH_HEAD").strip()
        head, base = t.commit[:8], base or "commit trước đó"
        base_sha = _parent_or_empty(dest, head_sha)
    else:
        branch = t.branch or base or default_branch(dest)
        base = base or (default_branch(dest) if t.branch else branch)
        if branch == base:
            # chỉ có link repo (hoặc nhánh trùng nhánh gốc): xem commit mới nhất của nhánh đó
            step(f"tải commit mới nhất của {t.owner}/{t.repo} ({branch})")
            _git(dest, "fetch", "-q", "--depth", "2", "origin", branch)
            head_sha = _git(dest, "rev-parse", "FETCH_HEAD").strip()
            head, base = f"{branch} (mới nhất)", "commit trước đó"
            base_sha = _parent_or_empty(dest, head_sha)
        else:
            step(f"tải nhánh {branch} của {t.owner}/{t.repo}")
            _git(dest, "fetch", "-q", "--depth", "1", "origin", branch)
            head_sha = _git(dest, "rev-parse", "FETCH_HEAD").strip()
            head = branch

    if not base_sha:
        if not REF_RE.match(base) or ".." in base:
            raise InputError(f"Tên nhánh gốc không hợp lệ: {base!r}")
        step(f"tải nhánh gốc {base}")
        _git(dest, "fetch", "-q", "--depth", "1", "origin", base)
        base_sha = _git(dest, "rev-parse", "FETCH_HEAD").strip()

    _git(dest, "checkout", "-q", head_sha)
    diff = _git(dest, "diff", "--no-color", base_sha, head_sha)
    if not diff.strip():
        raise InputError(f"Không có thay đổi nào giữa '{base}' và '{head}'. Hãy chọn một PR hoặc nhánh có sửa đổi.")
    if len(diff) > MAX_DIFF_CHARS:
        raise InputError(f"Thay đổi quá lớn ({len(diff):,} ký tự, tối đa {MAX_DIFF_CHARS:,}). "
                         "Hãy chọn PR nhỏ hơn hoặc dán riêng phần cần xem.")
    diff_path = os.path.join(workdir, "input.diff")
    with open(diff_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(diff)
    return Prepared(dest, base, head, diff_path, t.label, head_sha=head_sha, title=title)


def _parent_or_empty(repo_dir: str, sha: str) -> str:
    try:
        return _git(repo_dir, "rev-parse", f"{sha}~1").strip()
    except InputError:
        return EMPTY_TREE            # commit đầu tiên của repo: so với "không có gì"


# ---------------------------------------------------------------- diff / code dán vào

def prepare_diff_text(text: str, workdir: str) -> Prepared:
    text = (text or "").replace("\r\n", "\n")
    if not text.strip():
        raise InputError("Chưa có nội dung diff.")
    if len(text) > MAX_TEXT_CHARS:
        raise InputError(f"Diff quá dài ({len(text):,} ký tự, tối đa {MAX_TEXT_CHARS:,}).")
    if not re.search(r"^(diff --git |\+\+\+ |@@ )", text, re.M):
        raise InputError("Không nhận ra định dạng diff. Hãy dán kết quả của lệnh `git diff`, "
                         "hoặc chuyển sang ô 'Dán code' nếu bạn chỉ có đoạn code.")
    repo = os.path.join(workdir, "src")
    os.makedirs(repo, exist_ok=True)
    diff_path = os.path.join(workdir, "input.diff")
    with open(diff_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    return Prepared(repo, "trước", "sau", diff_path, "Diff dán vào")


_GUESS = [   # thứ tự = ưu tiên; mẫu Python đặt trước JS vì cả hai đều có `import`
    ("Dockerfile", re.compile(r"^\s*FROM\s+\S+", re.M)),
    ("code.go", re.compile(r"^\s*package\s+\w+|^\s*func\s+\w+\(", re.M)),
    ("Main.java", re.compile(r"^\s*(public\s+)?(class|interface)\s+\w+\s*(\{|extends|implements)|^\s*import\s+java\.", re.M)),
    ("code.ts", re.compile(r":\s*(string|number|boolean)\b|^\s*interface\s+\w+|^\s*export\s+type\b", re.M)),
    ("code.py", re.compile(r"^\s*(def|class)\s+\w+.*:\s*$|^\s*from\s+[\w.]+\s+import\b|^\s*import\s+[\w.]+(\s*,\s*[\w.]+)*\s*$|print\(", re.M)),
    ("code.js", re.compile(r"^\s*(const|let|var|function|export)\b|^\s*import\s.*\bfrom\s+['\"]|=>|console\.log", re.M)),
    ("code.sql", re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE)\b", re.I | re.M)),
]


def guess_filename(code: str) -> str:
    """Đoán tên file theo nội dung để router chọn đúng vai và checklist; không chắc thì .txt."""
    for name, rx in _GUESS:
        if rx.search(code):
            return name
    return "code.txt"


def code_to_diff(code: str, filename: str) -> str:
    lines = code.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    body = "\n".join("+" + ln for ln in lines)
    return (f"diff --git a/{filename} b/{filename}\nnew file mode 100644\n--- /dev/null\n+++ b/{filename}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n{body}\n")


def prepare_code_text(code: str, filename: str, workdir: str) -> Prepared:
    code = (code or "").replace("\r\n", "\n")
    if not code.strip():
        raise InputError("Chưa có đoạn code nào.")
    if len(code) > MAX_TEXT_CHARS:
        raise InputError(f"Đoạn code quá dài ({len(code):,} ký tự, tối đa {MAX_TEXT_CHARS:,}).")
    filename = (filename or "").strip() or guess_filename(code)
    if not FILENAME_RE.match(filename):
        raise InputError("Tên file chỉ gồm chữ, số, dấu chấm, gạch dưới, gạch ngang (ví dụ app.py, Dockerfile).")
    repo = os.path.join(workdir, "src")
    os.makedirs(repo, exist_ok=True)
    with open(os.path.join(repo, filename), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(code if code.endswith("\n") else code + "\n")
    diff_path = os.path.join(workdir, "input.diff")
    with open(diff_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(code_to_diff(code, filename))
    return Prepared(repo, "trống", filename, diff_path, f"Đoạn code dán vào ({filename})")


def prepare(spec: dict, workdir: str, on_step=None) -> Prepared:
    """spec = {"source": github|diff|code, "url", "text", "filename", "base"} -> Prepared (ném InputError)."""
    src = spec.get("source")
    if src == "github":
        return fetch_github(parse_github_url(spec.get("url", "")), (spec.get("base") or "").strip(), workdir, on_step)
    if src == "diff":
        return prepare_diff_text(spec.get("text", ""), workdir)
    if src == "code":
        return prepare_code_text(spec.get("text", ""), spec.get("filename", ""), workdir)
    raise InputError(f"Nguồn không hỗ trợ: {src!r}")
