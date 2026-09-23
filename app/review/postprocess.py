"""Lọc, dedupe, xếp hạng và map finding -> comment GitHub."""
from app.review.schemas import Finding, SEVERITY_RANK
from app.review.diff_utils import ChangedFile, commentable_line

SEV_EMOJI = {"critical": "🚨", "high": "🔴", "medium": "🟠", "low": "🟡"}


def dedupe(findings: list[Finding]) -> list[Finding]:
    """Trùng file+dòng(±2)+category -> giữ cái có severity/confidence cao hơn (ưu tiên static tool)."""
    kept: list[Finding] = []
    for f in sorted(findings, key=lambda x: (x.source == "llm", -SEVERITY_RANK[x.severity], -x.confidence)):
        dup = any(k.file == f.file and abs(k.line - f.line) <= 2 and k.category == f.category for k in kept)
        if not dup:
            kept.append(f)
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
