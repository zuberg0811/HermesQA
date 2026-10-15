"""Lớp 5 của "tự học": hiệu chỉnh confidence theo lịch sử 👎 của repo. Thống kê thuần, không gọi LLM.

Ý tưởng: lớp 1 (app.feedback) chỉ ghi CHỮ vào prompt và trông chờ model nghe theo. Lớp này làm
một việc chắc chắn hơn: nếu repo đã 👎 nhiều lần với cùng một LOẠI nhận xét ở cùng một KHU VỰC,
thì nhận xét loại đó lần sau bị hạ confidence trước khi qua ngưỡng min_confidence — tức là
rơi khỏi comment inline mà không cần model "nhớ".

Khoá đếm (từ cụ thể đến chung, khớp khoá cụ thể trước):
  rule+path  <source>:<rule> | <thư mục cấp 1>     ví dụ  llm:style | scripts   hoặc  bandit:B101 | tests
  rule       <source>:<rule>                        ví dụ  ruff:E501
Với static tool, <rule> là mã luật ổn định (phần trước dấu ':' của title). Với LLM, title mỗi lần
một kiểu nên dùng category.

Không bao giờ hạ nhóm được bảo vệ (security / bug mức high trở lên, xem agent.is_protected):
đó là nơi bỏ sót đắt nhất; người 👎 nhầm một lần không được phép tắt cảnh báo bảo mật.
"""
import logging
from collections import defaultdict

from app.review.agent import is_protected
from app.review.schemas import Finding

log = logging.getLogger("hermesqa.calibrate")


def rule_key(f: Finding) -> str:
    if f.source == "llm":
        return f"llm:{f.category}"
    rule = f.title.split(":", 1)[0].strip() or f.title
    return f"{f.source}:{rule}"


def path_key(file: str) -> str:
    norm = file.replace("\\", "/").lstrip("./")
    return norm.split("/", 1)[0] if "/" in norm else "."


def keys_for(f: Finding) -> list[str]:
    r = rule_key(f)
    return [f"{r} | {path_key(f.file)}", r]


class Profile:
    """Bảng đếm 👍/👎 theo khoá, dựng từ danh sách phản hồi {kind, file, category, title, source}."""
    def __init__(self, feedback_rows: list[dict] | None = None):
        self.counts: dict[str, dict[str, int]] = defaultdict(lambda: {"reject": 0, "accept": 0})
        for row in feedback_rows or []:
            self.add(row)

    def add(self, row: dict):
        kind = row.get("kind")
        if kind not in ("reject", "accept"):
            return
        f = Finding(file=row.get("file") or "?", line=1, severity="low", category=row.get("category") or "?",
                    title=row.get("title") or "?", explanation="", source=row.get("source") or "llm")
        for k in keys_for(f):
            self.counts[k][kind] += 1

    def lookup(self, f: Finding) -> tuple[str, dict[str, int]] | None:
        """Khoá cụ thể nhất có dữ liệu."""
        for k in keys_for(f):
            if k in self.counts:
                return k, self.counts[k]
        return None

    def __len__(self):
        return len(self.counts)


def calibrate(findings: list[Finding], profile: Profile, min_rejects: int = 2, reject_ratio: float = 0.67,
              factor: float = 0.5) -> list[dict]:
    """Hạ confidence tại chỗ cho finding thuộc loại bị 👎 nhiều. Trả danh sách điều chỉnh (để log/report)."""
    adjustments: list[dict] = []
    if not profile or not findings:
        return adjustments
    for f in findings:
        if is_protected(f):
            continue
        hit = profile.lookup(f)
        if not hit:
            continue
        key, c = hit
        total = c["reject"] + c["accept"]
        if c["reject"] < min_rejects or total == 0 or c["reject"] / total < reject_ratio:
            continue
        before = f.confidence
        f.confidence = round(before * factor, 2)
        f.explanation = (f.explanation.rstrip() +
                         f"\n\n<sub>Confidence hạ {before:.0%} → {f.confidence:.0%}: repo này đã 👎 {c['reject']}/{total} lần "
                         f"với nhận xét loại `{key}`.</sub>")
        adjustments.append({"file": f.file, "line": f.line, "title": f.title, "key": key,
                            "reject": c["reject"], "accept": c["accept"], "before": before, "after": f.confidence})
    if adjustments:
        log.info("calibrated %d finding(s) from feedback: %s", len(adjustments),
                 "; ".join(f"{a['file']}:{a['line']} {a['before']:.0%}->{a['after']:.0%} ({a['key']})" for a in adjustments))
    return adjustments
