"""Phân loại finding KHÔNG khớp ground truth trên PR thật (AACR) bằng giám khảo LLM: đúng-nhưng-ngoài-nhãn hay sai.

Giống eval/judge_fp.py (bộ lỗi tiêm) nhưng cho eval/aacr_eval.py: ground truth của PR thật không đầy đủ, nên finding
không khớp chưa chắc sai. Giám khảo đọc diff + file và bỏ phiếu valid/invalid. Kết quả dùng hai việc:
  - precision "nới" (tính cả nhận xét đúng ngoài nhãn) bên cạnh precision theo nhãn;
  - finding bị kết luận SAI thành bằng chứng cho lớp 4 tự học (tools/learn_rules.py propose --evidence-file), thay cho
    👎 của người khi chưa có người dùng thật. Nên chạy với model KHÁC model đã sinh finding (vd không đặt LLM_PROFILE ->
    Gemini chấm finding của Claude) để tránh "tự đồng ý với mình".

Dùng:
  python eval/aacr_fp_judge.py --out out-claudecode --config llm --evidence-out data/rules_candidates/_evidence_aacr.json
"""
import argparse
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aacr_eval  # noqa: E402
from aacr_eval import WORK, load_prs, repo_dir, sh  # noqa: E402
from judge_fp import JUDGE_PROMPT, JUDGE_SYSTEM, numbered  # noqa: E402


def matched_in_caches(out_dir: str, pr_id: str) -> set[int]:
    """Chỉ số finding đã được BẤT KỲ giám khảo nào (cache mặc định hoặc _judge_cache_<tag>) khớp với một nhận xét chuẩn."""
    hit: set[int] = set()
    for name in os.listdir(out_dir):
        if name.startswith("_judge_cache") and name.endswith(".json"):
            for k, v in json.load(open(os.path.join(out_dir, name), encoding="utf-8")).items():
                if k.startswith(pr_id + "|") and isinstance(v, int) and v >= 0:
                    hit.add(v)
    return hit


def file_diff(diff_text: str, path: str) -> str:
    """Phần diff của riêng một file trong diff cả PR."""
    for chunk in diff_text.split("\ndiff --git "):
        head = chunk.splitlines()[0] if chunk else ""
        if head.rstrip().endswith(" b/" + path):
            return chunk if chunk.startswith("diff --git") else "diff --git " + chunk
    return ""


def judge_one(backend, pr: dict, f: dict, votes: int) -> dict:
    diff = file_diff(open(os.path.join(WORK, "diffs", pr["id"] + ".diff"), encoding="utf-8").read(), f["file"])
    content = sh(["git", "-C", repo_dir(pr), "show", f"{pr['head']}:{f['file']}"], check=False)
    prompt = (JUDGE_PROMPT.replace("<<FILE>>", f["file"]).replace("<<LINE>>", str(f["line"]))
              .replace("<<CATEGORY>>", f.get("category", "")).replace("<<SEVERITY>>", f.get("severity", ""))
              .replace("<<TITLE>>", f.get("title", "")).replace("<<EXPLANATION>>", (f.get("explanation") or "")[:1200])
              .replace("<<DIFF>>", diff[:12000]).replace("<<CONTENT>>", numbered(content)[:20000]))
    ballots = []
    for _ in range(votes):
        for attempt in range(6):          # API quá tải: chờ rồi gọi lại (bài học Gemini 503 ngày 02/11)
            try:
                d = backend.run_json("SE", prompt, system=JUDGE_SYSTEM)
                verdict = str(d.get("verdict", "")).strip().lower()
                if verdict in ("valid", "invalid"):
                    ballots.append({"verdict": verdict, "introduced_by_diff": bool(d.get("introduced_by_diff")),
                                    "reason": str(d.get("reason", ""))[:300]})
                break
            except Exception as e:
                print(f"    phiếu lỗi (lần {attempt + 1}): {str(e)[:160]}", flush=True)
                time.sleep(5 * (attempt + 1))
    if len(ballots) * 2 <= votes:
        return {"verdict": "unknown", "introduced_by_diff": None, "ballots": ballots}
    tally = Counter(b["verdict"] for b in ballots)
    verdict = "valid" if tally["valid"] > tally["invalid"] else "invalid" if tally["invalid"] > tally["valid"] else "unknown"
    return {"verdict": verdict, "introduced_by_diff": sum(b["introduced_by_diff"] for b in ballots) * 2 > len(ballots),
            "ballots": ballots}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out", help="thư mục kết quả dưới eval/aacr/ (vd. out-claudecode)")
    ap.add_argument("--config", default="llm")
    ap.add_argument("--votes", type=int, default=3)
    ap.add_argument("--lang", default="Python")
    ap.add_argument("--max-lines", type=int, default=210)
    ap.add_argument("--review-model", default="", help="tên model đã sinh finding (chỉ để ghi vào báo cáo)")
    ap.add_argument("--evidence-out", help="ghi finding bị kết luận SAI thành JSON bằng chứng cho tools/learn_rules.py propose")
    a = ap.parse_args()
    aacr_eval.OUT_NAME = a.out

    from app.config import settings
    from app.review.agent import get_backend
    backend = get_backend()
    judge_model = settings.anthropic_model if settings.agent_backend == "anthropic" else settings.llm_model
    out_dir = os.path.join(WORK, a.out, a.config)
    res_path = os.path.join(out_dir, "fp_judgement.json")
    done = {(r["case"], r["key"]): r for r in (json.load(open(res_path, encoding="utf-8")) if os.path.exists(res_path) else [])}

    prs = load_prs(a.lang, a.max_lines)
    records, tp_total, n_all = [], 0, 0
    for pr in prs.values():
        fpath = os.path.join(out_dir, pr["id"] + ".json")
        if not os.path.exists(fpath):
            continue
        findings = json.load(open(fpath, encoding="utf-8"))
        n_all += len(findings)
        used = matched_in_caches(out_dir, pr["id"])
        tp_total += len(used)
        for i, f in enumerate(findings):   # nội dung file đọc bằng `git show <head>:<path>`, không cần checkout
            if i in used:
                continue
            key = f"{f['file']}:{f['line']}:{f.get('source', '')}:{(f.get('title') or '')[:60]}"
            rec = done.get((pr["id"], key))
            if rec is None or rec.get("verdict") == "unknown":
                print(f"[{pr['id']}] {f.get('source')} {f['file']}:{f['line']} {(f.get('title') or '')[:60]}", flush=True)
                rec = {"case": pr["id"], "repo": f"{pr['owner']}/{pr['repo']}", "key": key, "file": f["file"], "line": f["line"],
                       "source": f.get("source"), "role": f.get("role"), "category": f.get("category"), "severity": f.get("severity"),
                       "title": f.get("title"), "judge_model": judge_model, **judge_one(backend, pr, f, a.votes)}
                done[(pr["id"], key)] = rec
                json.dump(list(done.values()), open(res_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            records.append(rec)

    v = Counter(r["verdict"] for r in records)
    pre = sum(1 for r in records if r["verdict"] == "valid" and not r["introduced_by_diff"])
    total = tp_total + len(records)
    lines = [f"# Phân loại finding không khớp nhãn — AACR `{a.config}` ({a.out}); model review `{a.review_model or '?'}`, "
             f"giám khảo `{judge_model}`, {a.votes} phiếu", "",
             f"- Finding: {n_all}; khớp nhãn (TP theo bất kỳ giám khảo nào): {tp_total}; không khớp: {len(records)}",
             f"- Không khớp nhưng ĐÚNG về code (valid): **{v['valid']}** — trong đó {pre} là vấn đề có sẵn, không do PR gây ra",
             f"- Không khớp và SAI (invalid): **{v['invalid']}**" + (f" · không kết luận: {v['unknown']}" if v["unknown"] else "")]
    if total:
        lines.append(f"- Precision theo nhãn: {tp_total / total:.1%} · nếu tính cả nhận xét đúng ngoài nhãn: "
                     f"**{(tp_total + v['valid']) / total:.1%}** · tỉ lệ nhận xét sai trên tổng: **{v['invalid'] / total:.1%}**")
    bad = [r for r in records if r["verdict"] == "invalid"]
    if bad:
        lines += ["", "## Finding bị giám khảo cho là SAI", ""]
        lines += [f"- `{r['case']}` {r['file']}:{r['line']} [{r['category']}] {r['title']} — {r['ballots'][0]['reason'] if r['ballots'] else ''}"
                  for r in bad]
    ok = [r for r in records if r["verdict"] == "valid"]
    if ok:
        lines += ["", "## Finding đúng nhưng ngoài nhãn", ""]
        lines += [f"- `{r['case']}` {r['file']}:{r['line']} [{r['category']}] {r['title']}" + ("" if r["introduced_by_diff"] else " (có sẵn)")
                  for r in ok]
    md = os.path.join(out_dir, "fp_judgement.md")
    open(md, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))
    if a.evidence_out:
        ev = [{"repo": r["repo"], "file": r["file"], "line": r["line"], "category": r["category"], "title": r["title"],
               "reason": f"giám khảo {judge_model} ({a.votes} phiếu) kết luận sai: {r['ballots'][0]['reason'] if r['ballots'] else ''}"}
              for r in bad]
        os.makedirs(os.path.dirname(os.path.abspath(a.evidence_out)), exist_ok=True)
        json.dump(ev, open(a.evidence_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n{len(ev)} bằng chứng -> {a.evidence_out}")


if __name__ == "__main__":
    main()
