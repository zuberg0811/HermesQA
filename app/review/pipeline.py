"""Phần LLM của pipeline review, dùng CHUNG cho CLI (eval) và worker (GitHub).

Trước đây cli.py và worker.py mỗi bên tự chép một bản các bước này, nên một sửa đổi có thể
được nối vào bên này mà quên bên kia (đã xảy ra với bộ lọc nhiễu bandit — bug D3). Gom về một
hàm để "đường eval đo" và "đường người dùng đi" là cùng một đường.

Thứ tự bước (phần tất định in hoa):
  1. CHỌN FILE gửi LLM (loại secret / generated)            selection.select_for_llm
  2. CHE dòng static tool đánh dấu là bí mật                diff_utils.render_patch(redact)
  3. CHIA FILE theo vai, CHIA LÔ theo ngân sách ký tự       selection.files_for_role / build_batches
  4. LLM review từng lô, kèm checklist theo loại file       agent + rules
  5. LOẠI finding ngoài phạm vi lô, GHIM bằng existing_code diff_utils.anchor_finding
  6. kiểm tra sự thật (LLM nêu căn cứ, PHẦN MỀM kiểm căn cứ) agent.verify
"""
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app.config import settings
from app.review.agent import AgentBackend, build_user_prompt, verify
from app.review.diff_utils import ChangedFile, anchor_finding, file_context, render_patch
from app.review.rules import resolve_rules
from app.review.schemas import Finding
from app.review.selection import Skipped, build_batches, files_for_role, select_for_llm

log = logging.getLogger("hermesqa.pipeline")

STATIC_SUMMARY_LIMIT = 40


@dataclass
class LLMReview:
    findings: list[Finding] = field(default_factory=list)
    summaries: dict[str, str] = field(default_factory=dict)
    failed_roles: list[str] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)      # file không được (hoặc chỉ được một phần) review
    removed: list[dict] = field(default_factory=list)         # finding bị loại sau khi sinh, kèm lý do
    stats: Counter = field(default_factory=Counter)

    def meta(self) -> dict:
        return {"skipped": [s.__dict__ for s in self.skipped], "removed": self.removed,
                "stats": dict(self.stats), "failed_roles": self.failed_roles}


def static_summary_for(static: list[Finding], paths: set[str]) -> str:
    rel = [f for f in static if f.file in paths]
    lines = [f"- {f.file}:{f.line} [{f.severity}] {f.title}: {f.explanation[:120]}" for f in rel[:STATIC_SUMMARY_LIMIT]]
    if len(rel) > STATIC_SUMMARY_LIMIT:
        lines.append(f"- (... và {len(rel) - STATIC_SUMMARY_LIMIT} phát hiện static khác không liệt kê ở đây)")
    return "\n".join(lines)


def _fix_path(path: str, known: list[str]) -> str:
    """LLM hay thêm 'a/', 'b/', './' vào đường dẫn. Đưa về đường dẫn có thật nếu khớp duy nhất."""
    p = path.replace("\\", "/").strip()
    if p in known:
        return p
    for prefix in ("./", "a/", "b/", "/"):
        if p.startswith(prefix) and p[len(prefix):] in known:
            return p[len(prefix):]
    tail = [k for k in known if k.endswith("/" + p) or p.endswith("/" + k)]
    return tail[0] if len(tail) == 1 else p


def scope_static_to_diff(static: list[Finding], files: list[ChangedFile]) -> list[Finding]:
    """Chỉ giữ finding static nằm TRONG hunk của diff (dòng thêm mới hoặc dòng ngữ cảnh quanh đó).

    Công cụ static quét cả file, nên trên repo thật phần lớn cảnh báo rơi vào code không hề thay đổi
    (pilot AACR: 240/275 finding static ngoài hunk). Chúng không phải việc của PR này, và GitHub cũng
    từ chối đăng review comment lên dòng ngoài hunk. File không có trong diff thì bỏ.
    """
    by_path = {f.path: f for f in files}
    kept = []
    for s in static:
        cf = by_path.get(s.file)
        if cf is not None and s.line in cf.new_lines:
            kept.append(s)
    dropped = len(static) - len(kept)
    if dropped:
        log.info("bỏ %d finding static nằm ngoài hunk của diff", dropped)
    return kept


def attach_code_to_static(static: list[Finding], files: list[ChangedFile]) -> None:
    """Gắn nội dung dòng vào finding static (dùng cho dấu vân tay: số dòng trôi giữa các lần push, nội dung thì không)."""
    by_path = {f.path: f for f in files}
    for s in static:
        cf = by_path.get(s.file)
        if cf and not s.existing_code:
            s.existing_code = cf.new_lines.get(s.line, "")
            s.anchor = "tool"


def run_llm_review(backend: AgentBackend, roles: list[str], files: list[ChangedFile], repo_dir: str,
                   pr_meta: dict, static: list[Finding], conventions: str = "", repo_memory: str = "",
                   verify_mode: str | None = None) -> LLMReview:
    mode = verify_mode or settings.verify_mode
    res = LLMReview()
    kept, res.skipped = select_for_llm(files)
    by_path = {f.path: f for f in kept}

    redact: dict[str, set[int]] = defaultdict(set)
    for s in static:
        if s.source == "gitleaks":
            redact[s.file].add(s.line)
    res.stats["redacted_lines"] = sum(len(v) for v in redact.values())

    seen_trunc: set[str] = set()
    for role in roles:
        role_files = files_for_role(role, kept)
        if not role_files:
            res.summaries[role] = "Không có file thuộc phạm vi của vai này."
            continue
        items = [{"path": f.path,
                  "patch": render_patch(f, redact[f.path]),
                  "context": file_context(repo_dir, f.path, f.added_lines, redact=redact[f.path])}
                 for f in role_files]
        batches, truncated = build_batches(items, settings.max_prompt_chars, settings.max_file_chars)
        for t in truncated:
            if t.path not in seen_trunc:
                seen_trunc.add(t.path)
                res.skipped.append(t)

        notes: list[str] = []
        try:
            for batch in batches:
                paths = [it["path"] for it in batch]
                prompt = build_user_prompt(pr_meta, batch, static_summary_for(static, set(paths)),
                                           conventions, repo_memory, resolve_rules(paths))
                out = backend.run(role, prompt)
                res.stats["llm_calls"] += 1
                res.stats["raw_findings"] += len(out.findings)

                in_scope = []
                for f in out.findings:
                    f.file = _fix_path(f.file, paths)
                    if f.file not in paths:
                        res.stats["dropped_out_of_scope"] += 1
                        res.removed.append({"role": role, "file": f.file, "line": f.line, "title": f.title,
                                            "ground": "scope", "check": "file không nằm trong lô được giao"})
                        continue
                    res.stats["anchor_" + anchor_finding(f, by_path[f.file])] += 1
                    in_scope.append(f)
                out.findings = in_scope

                if mode != "off" and out.findings:
                    res.stats["verify_calls"] += 1
                out, removed = verify(mode, backend, role, out, batch, by_path)
                res.removed += removed
                res.stats["removed_by_factcheck"] += len(removed)
                for f in out.findings:
                    f.file = _fix_path(f.file, paths)
                    if mode == "legacy":            # bản cũ sinh lại finding nên phải ghim lại
                        anchor_finding(f, by_path.get(f.file))
                    f.role, f.source = role, "llm"
                res.findings += out.findings
                if out.summary:
                    notes.append(out.summary)
        except Exception as e:  # một vai lỗi (LLM quá tải, JSON hỏng) không được làm hỏng cả review
            log.exception("role %s failed: %s", role, e)
            res.failed_roles.append(role)
            res.summaries[role] = f"(lỗi khi chạy: {e})"
            continue
        res.summaries[role] = " ".join(notes) or "Không có nhận xét."
    return res
