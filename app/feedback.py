"""Học từ phản hồi của người trên PR (lớp 1 của "tự học"): phần GHI vào repo memory.

Phần ĐỌC: worker nạp ghi chú liên quan tới từng lô file (memory.memory_for_batch, lớp 3) vào prompt
của mọi vai (agent.build_user_prompt). Module này điền vào bảng đó từ hai kênh:

1. reply   – người trả lời thẳng vào comment của bot ("không phải lỗi", "bỏ qua", "đúng rồi"...).
             GitHub gửi webhook pull_request_review_comment; main.py đẩy vào queue, worker.ingest_feedback gọi vào đây.
2. reaction – 👍/👎 trên comment của bot. GitHub KHÔNG có webhook cho reaction, nên worker đọc lại
             reactions của các comment cũ mỗi khi PR được push tiếp (sync_reactions) — trước khi gọi LLM,
             để bài học áp dụng ngay cho lần review đó.

Mỗi comment của bot mang dấu ẩn `<!-- hermesqa:fp=<fingerprint> -->` (postprocess.to_github_comment).
Dấu này là nguồn sự thật để nối comment ↔ finding kể cả khi DB chưa có id comment.
"""
import logging
import re

from app import memory

log = logging.getLogger("hermesqa.feedback")

MARKER = "<!-- hermesqa:fp={fp} -->"
MARKER_RE = re.compile(r"<!--\s*hermesqa:fp=([0-9a-f]{8,40})\s*-->")

# Từ khoá phân loại reply. Ưu tiên "reject" (người nói đây không phải lỗi) vì đó là bài học đắt nhất:
# một báo động giả lặp lại làm người đọc bỏ bot. "accept" chỉ nhận khi câu rõ ràng.
_REJECT = [
    "👎", ":-1:", "-1", "false positive", "fp", "not a bug", "not an issue", "ignore", "wontfix", "won't fix",
    "by design", "intended", "intentional", "no fix", "nit", "noise",
    "không phải lỗi", "ko phải lỗi", "không phải vấn đề", "bỏ qua", "bo qua", "chấp nhận", "chap nhan",
    "cố ý", "co y", "đúng ý", "sai rồi", "báo nhầm", "bao nham", "không cần sửa", "ko cần sửa", "không đúng",
]
_ACCEPT = [
    "👍", ":+1:", "+1", "good catch", "nice catch", "fixed", "will fix", "fixing", "agreed", "agree", "correct",
    "valid", "true",
    "đúng rồi", "dung roi", "đúng", "chuẩn", "chuan", "đã sửa", "da sua", "sẽ sửa", "se sua", "cảm ơn", "cam on", "ok", "oke",
]


def _tokens(text: str) -> str:
    return " " + " ".join(text.lower().split()) + " "


def classify(text: str) -> str | None:
    """'reject' | 'accept' | None. Khớp cụm từ có khoảng trắng bao quanh để 'fp' không ăn vào 'fpdf'."""
    t = _tokens(text or "")
    if not t.strip():
        return None
    for kw in _REJECT:
        if f" {kw} " in t or (len(kw) > 3 and kw in t):
            return "reject"
    for kw in _ACCEPT:
        if f" {kw} " in t or (len(kw) > 3 and kw in t):
            return "accept"
    return None


def fingerprint_from_body(body: str) -> str | None:
    m = MARKER_RE.search(body or "")
    return m.group(1) if m else None


def classify_reactions(reactions: dict | None) -> list[str]:
    """Từ khối `reactions` của GitHub → danh sách kind có số đếm > 0."""
    if not reactions:
        return []
    kinds = []
    if int(reactions.get("-1", 0) or 0) > 0:
        kinds.append("reject")
    if int(reactions.get("+1", 0) or 0) > 0:
        kinds.append("accept")
    return kinds


def _excerpt(text: str, n: int = 120) -> str:
    t = " ".join((text or "").split())
    t = MARKER_RE.sub("", t).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def build_note(posted: dict, kind: str, quote: str = "", channel: str = "reply") -> str:
    """Ghi chú ngắn, tự đứng được, để nạp vào prompt lần sau.
    Lỗi bảo mật/bug nặng vẫn phải báo nếu tái diễn nơi khác: ghi chú chỉ là tham khảo, không phải lệnh tắt."""
    where = posted.get("file") or "?"
    what = f"[{posted.get('category') or '?'}] {posted.get('title') or '?'}"
    src = posted.get("source") or "llm"
    tag = "" if src == "llm" else f" (luật {src})"
    if kind == "reject":
        head = f"KHÔNG báo lại: `{where}` — {what}{tag}. Người review nói đây không phải vấn đề"
    else:
        head = f"Đã xác nhận ĐÚNG: `{where}` — {what}{tag}. Người review đồng ý"
    head += " (reaction 👎)" if channel == "reaction" and kind == "reject" else ""
    head += " (reaction 👍)" if channel == "reaction" and kind == "accept" else ""
    q = _excerpt(quote)
    if q and channel == "reply":
        head += f': "{q}"'
    if kind == "reject" and posted.get("category") == "security":
        head += ". Bảo mật: chỉ tham khảo, vẫn báo nếu gặp mẫu này ở chỗ khác"
    return head


def record(posted: dict, kind: str, channel: str, author: str = "", body: str = "") -> bool:
    """Ghi phản hồi + ghi chú memory. Trả True nếu là phản hồi mới (chưa ghi nhận)."""
    cid = posted.get("comment_id")
    if cid is None:
        log.warning("feedback without comment id (fp=%s) — skipped", posted.get("fingerprint"))
        return False
    if not memory.save_feedback(posted["repo"], posted["pr_number"], posted["fingerprint"], cid, kind, channel, author, body):
        return False
    note = build_note(posted, kind, body, channel)
    memory.add_memory(posted["repo"], note, source="feedback", kind=kind, fingerprint=posted["fingerprint"],
                      file=posted.get("file", ""), category=posted.get("category", ""))
    log.info("feedback %s PR#%s %s via %s by %s: %s", posted["repo"], posted["pr_number"], kind, channel, author or "?", note)
    return True


# ---------- các điểm nối với GitHub (gh = app.github_app.GitHubClient hoặc bản giả trong test) ----------

def _resolve_parent(gh, owner: str, repo: str, pr: int, parent_id: int) -> dict | None:
    """Comment của bot mà người dùng trả lời vào. Thử DB theo id; không có thì tải comment và đọc dấu ẩn."""
    posted = memory.find_posted_by_comment(parent_id)
    if posted:
        return posted
    try:
        parent = gh.get_review_comment(owner, repo, parent_id)
    except Exception as e:
        log.warning("cannot fetch parent comment %s: %s", parent_id, e)
        return None
    fp = fingerprint_from_body(parent.get("body", ""))
    if not fp:
        return None   # không phải comment của HermesQA
    posted = memory.find_posted_by_fingerprint(f"{owner}/{repo}", pr, fp)
    if posted:
        if posted.get("comment_id") is None:
            memory.set_comment_ids(f"{owner}/{repo}", pr, {fp: parent_id})
        posted["comment_id"] = parent_id
        return posted
    # DB không có (ví dụ DB bị xoá): vẫn học được từ path của comment
    return {"repo": f"{owner}/{repo}", "pr_number": pr, "fingerprint": fp, "comment_id": parent_id,
            "file": parent.get("path", ""), "category": "", "title": _excerpt(parent.get("body", ""), 80),
            "source": "llm", "severity": "", "role": ""}


def ingest_reply(gh, owner: str, repo: str, pr: int, comment: dict) -> str | None:
    """comment: payload `comment` của webhook pull_request_review_comment (hoặc tối thiểu
    {id, in_reply_to_id, body, user:{login,type}}). Trả kind đã ghi, hoặc None nếu bỏ qua."""
    parent_id = comment.get("in_reply_to_id")
    if not parent_id:
        return None
    user = comment.get("user") or {}
    if user.get("type") == "Bot":
        return None
    kind = classify(comment.get("body", ""))
    if not kind:
        log.info("reply %s not classified: %s", comment.get("id"), _excerpt(comment.get("body", ""), 60))
        return None
    posted = _resolve_parent(gh, owner, repo, pr, int(parent_id))
    if not posted:
        return None
    record(posted, kind, "reply", user.get("login", ""), comment.get("body", ""))
    return kind


def link_comment_ids(gh, owner: str, repo: str, pr: int, review_id: int | None) -> int:
    """Sau khi đăng review: lấy id của từng comment inline (qua dấu ẩn) và lưu vào posted_comments."""
    try:
        comments = gh.list_review_comments(owner, repo, pr)
    except Exception as e:
        log.warning("cannot list review comments: %s", e)
        return 0
    ids: dict[str, int] = {}
    for c in comments:
        if review_id is not None and c.get("pull_request_review_id") not in (None, review_id):
            continue
        fp = fingerprint_from_body(c.get("body", ""))
        if fp:
            ids[fp] = c["id"]
    return memory.set_comment_ids(f"{owner}/{repo}", pr, ids) if ids else 0


def sync_reactions(gh, owner: str, repo: str, pr: int) -> int:
    """Đọc 👍/👎 trên mọi comment của bot trong PR (lần push sau). Trả số phản hồi mới ghi."""
    try:
        comments = gh.list_review_comments(owner, repo, pr)
    except Exception as e:
        log.warning("cannot list review comments for reactions: %s", e)
        return 0
    full = f"{owner}/{repo}"
    n = 0
    for c in comments:
        fp = fingerprint_from_body(c.get("body", ""))
        if not fp:
            continue
        kinds = classify_reactions(c.get("reactions"))
        if not kinds:
            continue
        posted = memory.find_posted_by_fingerprint(full, pr, fp) or {
            "repo": full, "pr_number": pr, "fingerprint": fp, "file": c.get("path", ""), "category": "",
            "title": _excerpt(c.get("body", ""), 80), "source": "llm", "severity": "", "role": ""}
        if posted.get("comment_id") is None:
            memory.set_comment_ids(full, pr, {fp: c["id"]})
        posted["comment_id"] = c["id"]
        for kind in kinds:
            if record(posted, kind, "reaction"):
                n += 1
    return n
