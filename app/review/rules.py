"""Checklist theo LOẠI FILE, đưa vào prompt cùng với SKILL.md của vai.

SKILL.md trả lời "vai này quan tâm điều gì"; checklist ở đây trả lời "với loại file này,
báo gì và KHÔNG báo gì". Mỗi checklist có mục "Không báo" — thiếu mục này là nguồn của
phần lớn báo động giả đo được trên bộ eval (10/35 case bị báo "thiếu unit test").

Cấu trúc và một phần nội dung phỏng theo rule_docs của alibaba/open-code-review
(Apache-2.0), viết lại bằng tiếng Việt và rút gọn.
"""
import fnmatch
import os
from pathlib import Path

from app.review.router import is_test

RULES_DIR = Path(__file__).parent.parent / "skills" / "rules"

# (pattern trên basename hoặc trên cả đường dẫn, file checklist). Khớp đầu tiên thắng.
_PATH_RULES: list[tuple[str, str]] = [
    (".github/workflows/*", "ci.md"),
    ("*.gitlab-ci.yml", "ci.md"),
]
_NAME_RULES: list[tuple[str, str]] = [
    ("Dockerfile*", "dockerfile.md"),
    ("*.dockerfile", "dockerfile.md"),
    ("docker-compose*.y*ml", "dockerfile.md"),
    ("compose*.y*ml", "dockerfile.md"),
    ("requirements*.txt", "deps.md"),
    ("pyproject.toml", "deps.md"),
    ("package.json", "deps.md"),
    ("go.mod", "deps.md"),
    ("pom.xml", "deps.md"),
    ("build.gradle*", "deps.md"),
    ("Cargo.toml", "deps.md"),
    ("*.py", "python.md"),
    ("*.pyi", "python.md"),
    ("*.js", "js_ts.md"), ("*.jsx", "js_ts.md"), ("*.ts", "js_ts.md"), ("*.tsx", "js_ts.md"),
    ("*.mjs", "js_ts.md"), ("*.cjs", "js_ts.md"),
    ("*.go", "go.md"),
    ("*.java", "java.md"), ("*.kt", "java.md"),
]
DEFAULT_RULE = "default.md"
TEST_RULE = "tests.md"

PREAMBLE = (
    "Ưu tiên CHÍNH XÁC hơn bao phủ: chỉ nêu vấn đề khi bạn chắc đó là lỗi thật do THAY ĐỔI NÀY gây ra. "
    "Code cũ không bị diff đụng tới thì không báo, kể cả khi nó có vấn đề. "
    "Một báo động giả làm người đọc mất lòng tin nhiều hơn một lỗi nhỏ bị bỏ sót."
)


def rule_files_for(path: str) -> list[str]:
    """Danh sách checklist áp dụng cho một file: checklist ngôn ngữ (+ tests.md nếu là file test)."""
    norm = path.replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]
    lang = None
    for pat, doc in _PATH_RULES:
        if fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(norm, "*/" + pat):
            lang = doc
            break
    if lang is None:
        for pat, doc in _NAME_RULES:
            if fnmatch.fnmatch(base, pat):
                lang = doc
                break
    docs = [lang or DEFAULT_RULE]
    if is_test(norm):
        docs.append(TEST_RULE)
    return docs


# Lớp 4 tự học: overlay "Không báo" học được từ phản hồi, nằm riêng để diff/rollback được và không
# đụng checklist gốc. Biến môi trường trỏ sang thư mục ứng viên khi chạy eval kiểm định (tools/learn_rules.py).
LEARNED_DIR_ENV = "HERMESQA_LEARNED_RULES_DIR"
LEARNED_HEADING = "**Không báo (học được từ phản hồi, đã qua kiểm định eval):**"


def learned_dir() -> Path:
    return Path(os.environ.get(LEARNED_DIR_ENV) or (RULES_DIR / "learned"))


def read_learned(doc: str) -> str:
    try:
        return (learned_dir() / doc).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _read(doc: str) -> str:
    try:
        body = (RULES_DIR / doc).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    overlay = read_learned(doc)
    return f"{body}\n\n{LEARNED_HEADING}\n{overlay}" if overlay else body


def resolve_rules(paths: list[str]) -> str:
    """Ghép các checklist cần cho một lô file. Mỗi checklist chỉ xuất hiện một lần,
    kèm danh sách file nó áp dụng để model biết luật nào cho file nào."""
    by_doc: dict[str, list[str]] = {}
    for p in paths:
        for doc in rule_files_for(p):
            by_doc.setdefault(doc, []).append(p)
    parts = [PREAMBLE]
    for doc, ps in by_doc.items():
        body = _read(doc)
        if not body:
            continue
        shown = ", ".join(ps[:6]) + (f" (+{len(ps) - 6} file)" if len(ps) > 6 else "")
        parts.append(f"## Áp dụng cho: {shown}\n{body}")
    return "\n\n".join(parts)
