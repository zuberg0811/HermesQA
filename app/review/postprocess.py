"""Lọc, dedupe, xếp hạng và map finding -> comment GitHub."""
import hashlib

from app.review.schemas import Finding, SEVERITY_RANK
from app.review.diff_utils import ChangedFile, commentable_line


def fingerprint(f: Finding) -> str:
    """Dấu vân tay ổn định giữa các lần chạy, để lần push sau không đăng lại comment cũ.

    Không dùng số dòng (trôi khi thêm/bớt code phía trên) và không dùng tiêu đề của LLM (mỗi lần
    chạy diễn đạt một kiểu). Dùng: file + loại + NỘI DUNG dòng code. Static tool có mã luật ổn định
    nên thêm mã luật để hai luật khác nhau trên cùng một dòng không trùng dấu.
    """
    code = " ".join(f.existing_code.split())
    if f.source == "llm":
        raw = f"{f.file}|llm|{f.category}|{code or ' '.join(f.title.lower().split())}"
    else:
        raw = f"{f.file}|{f.source}|{f.title}|{code or f.line}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def split_new(findings: list[Finding], seen: set[str]) -> tuple[list[Finding], list[Finding]]:
    """Tách (chưa đăng, đã đăng ở lần trước) theo dấu vân tay. Gán luôn f.fingerprint."""
    new, repeated = [], []
    for f in findings:
        f.fingerprint = f.fingerprint or fingerprint(f)
        (repeated if f.fingerprint in seen else new).append(f)
    return new, repeated

SEV_EMOJI = {"critical": "🚨", "high": "🔴", "medium": "🟠", "low": "🟡"}

# Hai finding cách nhau <= DEDUPE_LINE_TOL dòng trong cùng file được coi là cùng vị trí.
DEDUPE_LINE_TOL = 2


def _is_llm(f: Finding) -> bool:
    return f.source == "llm"


def _same_spot(a: Finding, b: Finding) -> bool:
    return a.file == b.file and abs(a.line - b.line) <= DEDUPE_LINE_TOL


def _is_duplicate(kept: Finding, new: Finding) -> bool:
    """Quy tắc gộp `new` vào `kept` (kept luôn đứng trước theo thứ tự sort: static trước LLM).

    - Cùng nguồn (static-static, LLM-LLM): phải cùng category. Hai vai LLM (SE, QA)
      cố ý báo hai khía cạnh khác nhau của cùng đoạn code (bug + thiếu test) —
      gộp theo vị trí sẽ mất lỗi thật (đo được: mất 2 TP trên bộ Python).
    - Khác nguồn (static tool vs LLM): cùng vị trí là đủ, VỚI điều kiện finding
      LLM không nghiêm trọng hơn finding static. Lý do gộp: hai bên dùng hai
      taxonomy khác nhau cho cùng một lỗi (ruff E711 gọi là "bug", LLM gọi là
      "maintainability"), và LLM đã được dặn KHÔNG lặp lại lỗi static nên thứ nó
      báo thêm ngay tại đó thường là nhận xét phụ về cùng đoạn code. Đòi cùng
      category làm bước gộp gần như vô hiệu (FP của `both` ~ tổng FP hai nguồn).
      Lý do có điều kiện severity: LLM tự chấm severity và hay thổi phồng; nếu
      nó cho rằng chỗ đó có lỗi nặng hơn thứ static thấy (semgrep "medium" cạnh
      nil-deref "critical"), đó là hai lỗi khác nhau — giữ cả hai. Nhờ vậy
      `conclusion()` (chỉ nhìn finding chính) không bao giờ bỏ sót critical.
    """
    if not _same_spot(kept, new):
        return False
    if kept.category == new.category:
        return True
    if _is_llm(kept) == _is_llm(new):
        return False
    return SEVERITY_RANK[new.severity] <= SEVERITY_RANK[kept.severity]


def _attach(primary: Finding, other: Finding) -> None:
    """Đính finding bị gộp vào finding chính để comment không mất thông tin."""
    note = " ".join(other.explanation.split())
    if len(note) > 300:
        note = note[:297] + "..."
    sep = chr(10) * 2
    primary.explanation = f"{primary.explanation.rstrip()}{sep}_Cùng vị trí ({other.role}): **{other.title}** — {note}_"


def dedupe(findings: list[Finding]) -> list[Finding]:
    """Gộp finding trùng vị trí (xem `_is_duplicate`), giữ 1 finding chính mỗi cụm.

    Finding chính là cái đứng trước theo thứ tự: static tool trước LLM (mô tả
    tất định, không bịa), rồi severity, rồi confidence. Cùng category mà hai
    bên cùng thấy thì severity của cụm lấy mức cao nhất — không hạ mức.
    Finding bị gộp được đính vào explanation của finding chính.
    """
    kept: list[Finding] = []
    for f in sorted(findings, key=lambda x: (_is_llm(x), -SEVERITY_RANK[x.severity], -x.confidence)):
        k = next((k for k in kept if _is_duplicate(k, f)), None)
        if k is None:
            kept.append(f)
            continue
        if k.category == f.category and SEVERITY_RANK[f.severity] > SEVERITY_RANK[k.severity]:
            k.severity = f.severity
        _attach(k, f)
    return kept


def apply_policy(findings: list[Finding], files: list[ChangedFile], min_conf: float, min_sev: str, max_comments: int):
    by_path = {f.path: f for f in files}
    inline, overflow = [], []
    for f in sorted(findings, key=lambda x: (-SEVERITY_RANK[x.severity], -x.confidence)):
        if f.confidence < min_conf or SEVERITY_RANK[f.severity] < SEVERITY_RANK[min_sev]:
            continue
        cf = by_path.get(f.file)
        line = commentable_line(cf, f.line) if cf else None
        if line and len(inline) < max_comments:
            f.line = line
            inline.append(f)
        else:
            overflow.append(f)
    return inline, overflow


def to_github_comment(f: Finding) -> dict:
    body = f"{SEV_EMOJI[f.severity]} **[{f.severity.upper()} · {f.category}] {f.title}**\n\n{f.explanation}"
    if f.suggested_fix:
        fix = f.suggested_fix.strip()
        body += f"\n\n**Gợi ý sửa:**\n```\n{fix}\n```" if "\n" in fix or len(fix) > 60 else f"\n\n**Gợi ý sửa:** {fix}"
    body += f"\n\n<sub>{f.role} · confidence {f.confidence:.0%}</sub>"
    return {"path": f.file, "line": f.line, "side": "RIGHT", "body": body}


def conclusion(findings: list[Finding], block_on_critical: bool) -> str:
    has_crit = any(f.severity == "critical" for f in findings)
    if has_crit and block_on_critical:
        return "failure"
    if has_crit or any(f.severity == "high" for f in findings):
        return "neutral"
    return "success"
