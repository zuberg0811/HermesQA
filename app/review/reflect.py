"""Lớp 2 của "tự học": sau mỗi lần review, agent tự rút ghi chú về REPO và ghi vào repo memory.

Giống cơ chế "agent-curated memory" của Hermes Agent: không chờ người phản hồi (lớp 1), agent tự
nhìn lại lần chạy — nhận xét nào được giữ, nhận xét nào bị fact-check loại và vì sao — rồi viết
0–3 ghi chú ngắn cho lần sau. Ghi chú đi vào cùng bảng repo_memory, nguồn "agent", và được nạp
vào prompt của mọi vai ở lần review sau (memory.get_repo_memory).

Hàng rào, vì agent tự viết cho chính nó đọc:
- chỉ ghi chú về repo (convention, cấu trúc, thứ không nên báo), không về PR vừa review;
- ghi chú "không báo" về bảo mật bị bỏ — chỉ người (lớp 1) mới được dạy điều đó;
- lọc trùng với ghi chú đang có; mỗi lần tối đa 3; tổng ghi chú agent mỗi repo có trần (prune),
  ghi chú do người dạy không bao giờ bị cắt;
- lỗi ở bước này không được làm hỏng review (learn() nuốt lỗi và trả []).
"""
import json
import logging

from app import memory
from app.review.schemas import Finding

log = logging.getLogger("hermesqa.reflect")

KINDS = ("convention", "false_positive", "pattern")
MAX_NOTES_PER_RUN = 3
MIN_LEN, MAX_LEN = 15, 240

SYSTEM = """Bạn là bộ nhớ dài hạn của HermesQA — bot review pull request — cho MỘT repo cụ thể.
Sau mỗi lần review, bạn rút ra tối đa 3 ghi chú NGẮN về repo này để lần review sau chính xác hơn.

Ghi chú tốt nói về REPO, không nói về PR vừa review:
- convention: quy ước code của repo mà reviewer cần biết (vd "scripts/ dùng print() có chủ đích, không yêu cầu logging").
- false_positive: loại nhận xét đã bị kiểm chứng bác bỏ và có khả năng lặp lại (vd "helper trong tests/conftest.py
  đã có retry, đừng báo thiếu xử lý lỗi mạng ở test").
- pattern: cấu trúc/đặc thù của repo giúp review đúng hơn (vd "app/legacy/ là code cũ đang bị thay dần, chỉ báo lỗi nghiêm trọng").

KHÔNG viết:
- ghi chú nhắc lại một ghi chú đã có;
- ghi chú bảo bỏ qua lỗi bảo mật / secret / injection (chỉ người review mới được dạy điều đó);
- ghi chú về một dòng code cụ thể của PR này, hay khen/chê chung chung;
- ghi chú khi không có gì đáng nhớ: trả về danh sách rỗng là câu trả lời đúng trong đa số trường hợp.

Trả về đúng MỘT JSON object, không thêm chữ ngoài JSON:
{"notes": [{"note": "<một câu, <= 200 ký tự, tiếng Việt>", "kind": "convention|false_positive|pattern",
            "file": "<đường dẫn hoặc thư mục liên quan, có thể rỗng>", "category": "<security|bug|performance|test|style|devops hoặc rỗng>"}]}"""


def _fmt_findings(findings: list[Finding], limit: int = 30) -> str:
    if not findings:
        return "(không có)"
    lines = [f"- {f.file}:{f.line} [{f.severity}/{f.category}] {f.title} (nguồn {f.source}, confidence {f.confidence:.0%})"
             for f in findings[:limit]]
    if len(findings) > limit:
        lines.append(f"- … và {len(findings) - limit} nhận xét khác")
    return "\n".join(lines)


def _fmt_removed(removed: list[dict], limit: int = 30) -> str:
    if not removed:
        return "(không có)"
    lines = [f"- {r.get('file')}:{r.get('line')} {r.get('title')} — bị loại vì: {r.get('check') or r.get('ground') or '?'}"
             for r in removed[:limit]]
    if len(removed) > limit:
        lines.append(f"- … và {len(removed) - limit} nhận xét khác")
    return "\n".join(lines)


def build_prompt(pr_meta: dict, final: list[Finding], removed: list[dict], existing_notes: list[str],
                 conventions: str = "") -> str:
    parts = [f"# Repo: {pr_meta.get('owner', '?')}/{pr_meta.get('repo', '?')} — vừa review PR #{pr_meta.get('pr_number', '?')}: "
             f"{pr_meta.get('title', '')}"]
    if conventions:
        parts.append(f"\n# Quy ước repo tự khai (.hermesqa.yml)\n{conventions}")
    parts.append("\n# Ghi chú đang có trong bộ nhớ (KHÔNG nhắc lại)\n" +
                 ("\n".join(f"- {n}" for n in existing_notes) if existing_notes else "(trống)"))
    parts.append("\n# Nhận xét được GIỮ sau kiểm chứng\n" + _fmt_findings(final))
    parts.append("\n# Nhận xét bị fact-check LOẠI (và lý do) — nguồn tốt cho ghi chú false_positive\n" + _fmt_removed(removed))
    parts.append("\nRút ra tối đa 3 ghi chú về repo theo định dạng JSON đã nêu. Không có gì đáng nhớ thì trả {\"notes\": []}.")
    return "\n".join(parts)


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def sanitize(raw: dict | list | None, existing_notes: list[str]) -> list[dict]:
    """Lọc đầu ra của model: đúng kiểu, độ dài hợp lý, không trùng, không tắt bảo mật, tối đa 3."""
    items = raw.get("notes") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    seen = [_norm(n) for n in existing_notes]
    out: list[dict] = []
    for it in items:
        if isinstance(it, str):
            it = {"note": it}
        if not isinstance(it, dict):
            continue
        note = " ".join(str(it.get("note") or "").split())
        if not (MIN_LEN <= len(note) <= MAX_LEN):
            continue
        kind = it.get("kind") if it.get("kind") in KINDS else "pattern"
        category = str(it.get("category") or "").strip().lower()
        if kind == "false_positive" and category == "security":
            log.info("reflect: dropped security false_positive note: %s", note)
            continue
        n = _norm(note)
        if any(n == s or (len(n) > 20 and (n in s or s in n)) for s in seen):
            continue
        seen.append(n)
        out.append({"note": note, "kind": kind, "file": str(it.get("file") or "")[:200], "category": category[:40]})
        if len(out) >= MAX_NOTES_PER_RUN:
            break
    return out


def reflect(backend, pr_meta: dict, final: list[Finding], removed: list[dict], existing_notes: list[str],
            conventions: str = "") -> list[dict]:
    """Một lần gọi LLM -> ghi chú đã lọc. Không ghi DB."""
    prompt = build_prompt(pr_meta, final, removed, existing_notes, conventions)
    data = backend.run_json("SE", prompt, system=SYSTEM)
    return sanitize(data, existing_notes)


def learn(backend, repo: str, pr_meta: dict, final: list[Finding], removed: list[dict], conventions: str = "",
          max_notes: int = 30) -> list[dict]:
    """Điểm gọi từ worker: phản tư -> ghi repo_memory (nguồn agent) -> cắt bớt ghi chú agent cũ. Nuốt lỗi."""
    if not final and not removed:
        return []
    try:
        existing = [r["note"] for r in memory.list_memory(repo, limit=60)]
        notes = reflect(backend, pr_meta, final, removed, existing, conventions)
        for n in notes:
            memory.add_memory(repo, n["note"], source="agent", kind=n["kind"], file=n["file"], category=n["category"])
        if notes:
            memory.prune_agent_notes(repo, keep=max_notes)
            log.info("reflect: learned %d note(s) for %s: %s", len(notes), repo, json.dumps(notes, ensure_ascii=False))
        return notes
    except Exception as e:  # học hỏi không được làm hỏng review
        log.warning("reflect failed for %s: %s", repo, e)
        return []
