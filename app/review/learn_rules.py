"""Lớp 4 của "tự học": tự đề xuất bổ sung mục "Không báo" của checklist, có CỔNG KIỂM ĐỊNH bằng eval.

Khác ba lớp trước (chỉ tác động một repo), checklist trong app/skills/rules áp dụng cho MỌI repo,
nên không được để LLM sửa thẳng. Quy trình:

  1. collect_evidence  gom bằng chứng từ DB: 👎 của người (bảng feedback + posted_comments) và ghi chú
                       false_positive / convention agent tự rút ra, gom theo checklist (python.md, tests.md…).
  2. propose           một lần gọi LLM: checklist gốc + overlay hiện có + bằng chứng -> tối đa 5 dòng
                       "Không báo" mới; sanitize lọc dòng chạm bảo mật, trùng, quá dài.
  3. write_candidate   ghi overlay ỨNG VIÊN vào thư mục riêng (không chạm app/skills/rules/learned).
  4. gate              chạy eval với HERMESQA_LEARNED_RULES_DIR trỏ sang ứng viên, chấm bằng eval/score.py,
                       so với baseline: precision không giảm, recall không giảm quá ngưỡng -> promote;
                       không thì giữ ứng viên để người xem, checklist không đổi.
  5. promote           chép ứng viên vào app/skills/rules/learned/<doc> và ghi HISTORY.md (số liệu trước/sau,
                       dòng thêm) để rollback bằng git.

Đây chính là biện pháp cho rủi ro "học thuộc đề" đã nêu trong docs/DE_XUAT…: thứ học được phải qua
bộ kiểm tra tách biệt, và bộ dùng để chấm cổng nên khác bộ sinh ra bằng chứng.
"""
import datetime as _dt
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from app import memory
from app.review import rules

log = logging.getLogger("hermesqa.learn_rules")

MAX_NEW_LINES = 5
MIN_LEN, MAX_LEN = 20, 200
# Overlay không bao giờ được dạy bot im lặng về bảo mật: dòng chạm các từ này bị bỏ.
_SECURITY_WORDS = ("secret", "token", "password", "mật khẩu", "injection", "bảo mật", "security", "eval(", "exec(",
                   "shell=true", "pickle", "verify=false", "md5", "sha1", "api key", "api_key", "credential", "xss",
                   "csrf", "hardcode", "hard-code")

SYSTEM = """Bạn bảo trì checklist review code của HermesQA cho MỘT loại file. Checklist có mục "Báo" và "Không báo".
Nhiệm vụ: từ bằng chứng (nhận xét bị người review bác bỏ, ghi chú agent rút ra từ nhiều repo), đề xuất tối đa 5 dòng
MỚI cho mục "Không báo" để giảm báo động giả lặp lại mà KHÔNG làm bỏ sót lỗi thật.

Nguyên tắc:
- Chỉ đề xuất khi bằng chứng cho thấy một LOẠI nhận xét bị bác bỏ lặp lại (>= 2 lần, hoặc ở >= 2 repo). Một lần lẻ: bỏ qua.
- Viết thành quy tắc tổng quát cho loại file này, không nhắc tên repo/file cụ thể.
- KHÔNG đề xuất gì liên quan bảo mật, secret, injection, xác thực — những thứ đó chỉ người được quyết.
- Không nhắc lại dòng đã có trong checklist hay overlay.
- Không có gì đủ chắc: trả danh sách rỗng. Đó là câu trả lời đúng trong đa số trường hợp.

Trả về đúng MỘT JSON object: {"lines": ["<một câu, <= 200 ký tự, tiếng Việt, không có dấu '-' đầu dòng>", ...]}"""


# ---------- 1. bằng chứng ----------

def doc_for_file(path: str) -> str:
    """Checklist ngôn ngữ của một file (bỏ tests.md: file test nhận cả hai, nhưng bằng chứng xếp theo ngôn ngữ)."""
    docs = rules.rule_files_for(path)
    return docs[0] if docs else rules.DEFAULT_RULE


def collect_evidence(extra_notes: list[str] | None = None) -> dict[str, list[str]]:
    """{doc: [dòng bằng chứng]} từ DB. extra_notes: dòng người gõ tay (--note), xếp vào default.md."""
    ev: dict[str, list[str]] = {}
    for fb in memory.feedback_profile_all():
        if fb.get("kind") != "reject" or not fb.get("file"):
            continue
        line = f"[{fb['repo']}] {fb['file']} [{fb.get('category') or '?'}] \"{fb.get('title') or '?'}\" — người review: không phải lỗi"
        ev.setdefault(doc_for_file(fb["file"]), []).append(line)
    for n in memory.list_memory_all(kinds=("false_positive", "convention")):
        if not n.get("file"):
            continue
        line = f"[{n['repo']}] ({n['kind']}) {n['note']}"
        ev.setdefault(doc_for_file(n["file"]), []).append(line)
    for n in extra_notes or []:
        ev.setdefault(rules.DEFAULT_RULE, []).append(f"[tay] {n}")
    return ev


# ---------- 2. đề xuất ----------

def _norm(s: str) -> str:
    return " ".join(s.lower().lstrip("-• ").split())


def sanitize(raw: dict | list | None, base_text: str, overlay_text: str) -> list[str]:
    items = raw.get("lines") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    seen = {_norm(l) for l in (base_text + "\n" + overlay_text).splitlines() if l.strip()}
    out: list[str] = []
    for it in items:
        if not isinstance(it, str):
            continue
        line = " ".join(it.strip().lstrip("-• ").split())
        if not (MIN_LEN <= len(line) <= MAX_LEN):
            continue
        low = line.lower()
        if any(w in low for w in _SECURITY_WORDS):
            log.info("learn_rules: dropped security-related line: %s", line)
            continue
        n = _norm(line)
        if n in seen or any(len(n) > 30 and (n in s or s in n) for s in seen):
            continue
        seen.add(n)
        out.append(line)
        if len(out) >= MAX_NEW_LINES:
            break
    return out


def build_prompt(doc: str, base_text: str, overlay_text: str, evidence: list[str]) -> str:
    return "\n".join([
        f"# Checklist: {doc}", base_text or "(trống)",
        "\n# Overlay 'Không báo' đã học (KHÔNG nhắc lại)", overlay_text or "(trống)",
        f"\n# Bằng chứng ({len(evidence)} dòng, từ nhiều repo)", *(f"- {e}" for e in evidence[:80]),
        "\nĐề xuất tối đa 5 dòng 'Không báo' mới theo JSON đã nêu; không đủ bằng chứng thì {\"lines\": []}.",
    ])


def propose(backend, doc: str, evidence: list[str]) -> list[str]:
    base = (rules.RULES_DIR / doc).read_text(encoding="utf-8") if (rules.RULES_DIR / doc).exists() else ""
    overlay = rules.read_learned(doc)
    if not evidence:
        return []
    data = backend.run_json("SE", build_prompt(doc, base, overlay, evidence), system=SYSTEM)
    return sanitize(data, base, overlay)


# ---------- 3. ứng viên ----------

def write_candidate(doc: str, new_lines: list[str], candidate_dir: Path, learned: Path | None = None) -> Path:
    """Thư mục ứng viên = bản sao overlay hiện có + dòng mới cho `doc`, để eval chạy với TOÀN BỘ overlay."""
    learned = learned or (rules.RULES_DIR / "learned")
    candidate_dir.mkdir(parents=True, exist_ok=True)
    if learned.exists():
        for p in learned.glob("*.md"):
            if p.name != "HISTORY.md":
                shutil.copy(p, candidate_dir / p.name)
    current = (learned / doc).read_text(encoding="utf-8").strip() if (learned / doc).exists() else ""
    body = "\n".join(([current] if current else []) + [f"- {l}" for l in new_lines]).strip() + "\n"
    out = candidate_dir / doc
    out.write_text(body, encoding="utf-8")
    return out


# ---------- 4. cổng ----------

def metrics(out_dir: str, config: str, truth: str = os.path.join("eval", "ground_truth.json"), tol: int = 3) -> dict:
    """precision/recall/f1/tp/fp/fn của một thư mục eval, qua eval/score.py."""
    sys.path.insert(0, "eval")
    import score  # noqa: E402
    with open(truth, encoding="utf-8") as f:
        gt = json.load(f)
    s = score.score_dir(gt, out_dir, tol, [config])["configs"][config]
    return {k: s[k] for k in ("cases", "tp", "fp", "fn", "precision", "recall", "f1")}


def gate(baseline: dict, candidate: dict, max_recall_drop: float = 0.0, min_precision_gain: float = 0.0) -> tuple[bool, str]:
    """Chấp nhận khi precision tăng ít nhất min_precision_gain và recall không giảm quá max_recall_drop."""
    dp = candidate["precision"] - baseline["precision"]
    dr = candidate["recall"] - baseline["recall"]
    msg = (f"precision {baseline['precision']:.1%} -> {candidate['precision']:.1%} ({dp:+.1%}), "
           f"recall {baseline['recall']:.1%} -> {candidate['recall']:.1%} ({dr:+.1%}), "
           f"F1 {baseline['f1']:.1%} -> {candidate['f1']:.1%}")
    if candidate.get("cases", 1) != baseline.get("cases", 1):
        return False, f"số case khác nhau ({baseline.get('cases')} vs {candidate.get('cases')}): không so được. {msg}"
    if dp < min_precision_gain:
        return False, f"precision không đạt (cần +{min_precision_gain:.1%}). {msg}"
    if dr < -max_recall_drop:
        return False, f"recall giảm quá {max_recall_drop:.1%}. {msg}"
    return True, msg


def run_candidate_eval(candidate_dir: Path, out_dir: str, config: str, profile: str | None = None,
                       limit: int | None = None, cases: str | None = None, python: str = sys.executable) -> int:
    """Chạy eval/run_eval.py với overlay ứng viên (qua biến môi trường). Trả mã thoát."""
    cmd = [python, os.path.join("eval", "run_eval.py"), "--configs", config, "--out", out_dir]
    if profile:
        cmd += ["--profile", profile]
    if limit:
        cmd += ["--limit", str(limit)]
    if cases:
        cmd += ["--cases", cases]
    env = {**os.environ, rules.LEARNED_DIR_ENV: str(candidate_dir.resolve()), "PYTHONIOENCODING": "utf-8"}
    log.info("eval với overlay ứng viên: %s", " ".join(cmd))
    return subprocess.run(cmd, env=env).returncode


# ---------- 5. promote ----------

def promote(doc: str, candidate_dir: Path, before: dict, after: dict, new_lines: list[str],
            learned: Path | None = None, note: str = "") -> Path:
    learned = learned or (rules.RULES_DIR / "learned")
    learned.mkdir(parents=True, exist_ok=True)
    dst = learned / doc
    shutil.copy(candidate_dir / doc, dst)
    hist = learned / "HISTORY.md"
    stamp = _dt.date.today().isoformat()
    entry = [f"## {stamp} — {doc}",
             f"- precision {before['precision']:.1%} -> {after['precision']:.1%}, recall {before['recall']:.1%} -> {after['recall']:.1%}, "
             f"F1 {before['f1']:.1%} -> {after['f1']:.1%} ({after.get('cases', '?')} case)"]
    if note:
        entry.append(f"- {note}")
    entry += [f"- thêm: {l}" for l in new_lines] + [""]
    head = "" if hist.exists() else "# Lịch sử overlay checklist học được (rollback: git revert commit tương ứng)\n\n"
    with open(hist, "a", encoding="utf-8") as f:
        f.write(head + "\n".join(entry) + "\n")
    return dst
