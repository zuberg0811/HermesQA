"""Lớp 4 tự học: đề xuất bổ sung "Không báo" cho checklist từ phản hồi, rồi kiểm định bằng eval trước khi nhận.

Hai bước, chạy tay (mỗi bước tốn LLM; bước gate tốn cả bộ eval):

  # 1. Gom bằng chứng từ DB (👎 của người + ghi chú agent) và nhờ LLM đề xuất, ghi ứng viên ra data/rules_candidates/
  python tools/learn_rules.py propose --docs python.md,tests.md [--profile claudecode] [--note "scripts/ dùng print có chủ đích"]

  # 2. Chạy eval với overlay ứng viên, so với baseline; đạt thì chép vào app/skills/rules/learned/ + HISTORY.md
  python tools/learn_rules.py gate --doc python.md --baseline eval/out-v3-rescored --out eval/out-learned \
         --config llm [--profile claudecode] [--max-recall-drop 0.0] [--limit 10]

  # xem overlay đang dùng + lịch sử
  python tools/learn_rules.py show

Cổng: precision không giảm (mặc định) và recall không giảm quá --max-recall-drop. Baseline phải cùng cấu hình,
cùng model và cùng số case với lần chạy ứng viên, nếu không so sánh vô nghĩa. Bộ eval dùng cho cổng nên KHÁC
nguồn bằng chứng (bằng chứng đến từ PR thật, cổng chấm trên bộ lỗi tiêm) để tránh "học thuộc đề".
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import memory  # noqa: E402
from app.config import settings  # noqa: E402
from app.review import learn_rules, rules  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
DEFAULT_CANDIDATES = Path("data") / "rules_candidates"


def cmd_propose(a):
    from app.review.agent import get_backend
    if a.profile:
        settings.use_profile(a.profile)
    memory.init_db()
    ev = learn_rules.collect_evidence(a.note or [])
    docs = a.docs.split(",") if a.docs else sorted(ev)
    backend = get_backend()
    cand = Path(a.candidate_dir)
    summary = {}
    for doc in docs:
        lines = learn_rules.propose(backend, doc, ev.get(doc, []))
        summary[doc] = {"evidence": len(ev.get(doc, [])), "proposed": lines}
        if lines:
            p = learn_rules.write_candidate(doc, lines, cand)
            print(f"[{doc}] {len(ev.get(doc, []))} bằng chứng -> {len(lines)} dòng mới -> {p}")
            for l in lines:
                print(f"   - {l}")
        else:
            print(f"[{doc}] {len(ev.get(doc, []))} bằng chứng -> không đề xuất gì")
    (cand / "_proposal.json").parent.mkdir(parents=True, exist_ok=True)
    (cand / "_proposal.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_gate(a):
    cand = Path(a.candidate_dir)
    if not (cand / a.doc).exists():
        sys.exit(f"không có ứng viên {cand / a.doc}; chạy propose trước")
    current = set(rules.read_learned(a.doc).splitlines())
    new_lines = [l.lstrip("- ").strip() for l in (cand / a.doc).read_text(encoding="utf-8").splitlines()
                 if l.strip() and l not in current]
    before = learn_rules.metrics(a.baseline, a.config, a.truth)
    if not a.skip_eval:
        rc = learn_rules.run_candidate_eval(cand, a.out, a.config, a.profile, a.limit, a.cases)
        if rc != 0:
            sys.exit(f"run_eval thất bại (rc={rc}); không promote")
    after = learn_rules.metrics(a.out, a.config, a.truth)
    ok, why = learn_rules.gate(before, after, a.max_recall_drop, a.min_precision_gain)
    print(("ĐẠT  " if ok else "KHÔNG ĐẠT  ") + why)
    if ok:
        dst = learn_rules.promote(a.doc, cand, before, after, new_lines, note=a.note or "")
        print(f"đã promote -> {dst} (xem {dst.parent / 'HISTORY.md'}; commit để giữ, git revert để rollback)")
    else:
        print(f"giữ nguyên checklist; ứng viên vẫn ở {cand / a.doc} để xem tay")
    sys.exit(0 if ok else 2)


def cmd_show(a):
    d = rules.learned_dir()
    print(f"thư mục overlay: {d}")
    for p in sorted(d.glob("*.md")) if d.exists() else []:
        print(f"\n== {p.name}\n{p.read_text(encoding='utf-8').strip()}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose"); p.set_defaults(fn=cmd_propose)
    p.add_argument("--docs", help="python.md,tests.md… (mặc định: mọi checklist có bằng chứng)")
    p.add_argument("--profile"); p.add_argument("--candidate-dir", default=str(DEFAULT_CANDIDATES))
    p.add_argument("--note", action="append", help="bằng chứng gõ tay, lặp lại được")
    g = sub.add_parser("gate"); g.set_defaults(fn=cmd_gate)
    g.add_argument("--doc", required=True); g.add_argument("--baseline", required=True)
    g.add_argument("--out", default=os.path.join("eval", "out-learned")); g.add_argument("--config", default="llm")
    g.add_argument("--profile"); g.add_argument("--limit", type=int); g.add_argument("--cases")
    g.add_argument("--truth", default=os.path.join("eval", "ground_truth.json"))
    g.add_argument("--max-recall-drop", type=float, default=0.0); g.add_argument("--min-precision-gain", type=float, default=0.0)
    g.add_argument("--candidate-dir", default=str(DEFAULT_CANDIDATES)); g.add_argument("--note")
    g.add_argument("--skip-eval", action="store_true", help="đã chạy eval ứng viên rồi, chỉ chấm và so")
    s = sub.add_parser("show"); s.set_defaults(fn=cmd_show)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
