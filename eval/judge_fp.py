"""Phân loại báo động giả (FP) bằng giám khảo LLM bỏ phiếu: "đúng nhưng ngoài ground truth" hay "sai sự thật".

Vì sao: score.py tính MỌI finding không khớp ground truth là FP, kể cả nhận xét đúng (vd "base image chưa
ghim digest" trong một case tiêm SQL injection). Precision báo cáo vì thế chỉ là cận dưới, và không trả lời
được câu hỏi quan trọng hơn: bao nhiêu phần là model BỊA. Script này hỏi một giám khảo LLM về từng FP,
hỏi N lần và lấy đa số (cách AACR-Bench chấm: majority voting).

Mỗi FP được gán:
  valid   — điều nhận xét nói là ĐÚNG về code (dù nhỏ, dù không do thay đổi này gây ra)
  invalid — điều nhận xét nói là SAI sự thật (bịa, hiểu sai, code không như mô tả)
và thêm introduced_by_diff: vấn đề do chính thay đổi này gây ra hay đã có từ trước.

GIỚI HẠN (phải ghi trong báo cáo): giám khảo đang là CÙNG model với bên sinh nhận xét nên có thiên vị
"đồng ý với chính mình"; con số valid là cận trên. Muốn chặt hơn: dùng model khác làm giám khảo
(đổi LLM_MODEL/AGENT_BACKEND khi chạy script này) hoặc rà tay một mẫu.

Dùng:
  python eval/judge_fp.py --out eval/out-v3 --config both
  python eval/judge_fp.py --truth eval/ground_truth_multi.json --out eval/out-multi-v3 --config llm --votes 3
Ghi: <out>/fp_judgement_<config>.json (chạy lại sẽ dùng tiếp kết quả đã có).
"""
import argparse
import json
import os
import subprocess
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GITHUB_APP_ID", "1")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "x")

from score import load_json, matched_indices  # noqa: E402

JUDGE_SYSTEM = (
    "Bạn là kỹ sư phần mềm cấp cao làm giám khảo độc lập. Bạn đánh giá MỘT nhận xét review code "
    "dựa trên code được cung cấp. Bạn công tâm: không mặc định đồng ý, không mặc định bác bỏ."
)

JUDGE_PROMPT = """Một công cụ review tự động đã đưa ra nhận xét dưới đây về một thay đổi code.
Hãy đối chiếu nhận xét với code và phân loại.

- "valid": điều nhận xét khẳng định là ĐÚNG về code này — vấn đề có thật, kể cả khi nhỏ, kể cả khi
  nó đã tồn tại từ trước chứ không do thay đổi này gây ra.
- "invalid": điều nhận xét khẳng định là SAI — mô tả code không đúng, vấn đề không tồn tại, hoặc code
  đã xử lý điều mà nhận xét nói là thiếu.

Ngoài ra cho biết vấn đề (nếu có thật) có do CHÍNH thay đổi này (các dòng '+') gây ra hay không.

# Nhận xét
File: <<FILE>>, dòng <<LINE>>
Loại: <<CATEGORY>> · Mức: <<SEVERITY>>
Tiêu đề: <<TITLE>>
Giải thích: <<EXPLANATION>>

# Diff của file
```diff
<<DIFF>>
```

# Nội dung file sau thay đổi
```
<<CONTENT>>
```

Trả về DUY NHẤT một JSON object:
{"verdict": "valid hoặc invalid", "introduced_by_diff": true hoặc false, "reason": "một câu"}
"""


def git(repo: str, *args: str) -> str:
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.stdout if p.returncode == 0 else ""


def numbered(text: str, limit: int = 400) -> str:
    lines = text.splitlines()[:limit]
    return "\n".join(f"{i:4d}| {l}" for i, l in enumerate(lines, 1))


def judge_one(backend, repo: str, branch: str, f: dict, votes: int) -> dict:
    diff = git(repo, "diff", f"main...{branch}", "--", f["file"])
    content = git(repo, "show", f"{branch}:{f['file']}")
    prompt = (JUDGE_PROMPT.replace("<<FILE>>", f["file"]).replace("<<LINE>>", str(f["line"]))
              .replace("<<CATEGORY>>", f.get("category", "")).replace("<<SEVERITY>>", f.get("severity", ""))
              .replace("<<TITLE>>", f.get("title", "")).replace("<<EXPLANATION>>", f.get("explanation", "")[:1200])
              .replace("<<DIFF>>", diff[:12000]).replace("<<CONTENT>>", numbered(content)[:20000]))
    ballots = []
    for _ in range(votes):
        try:
            d = backend.run_json("SE", prompt, system=JUDGE_SYSTEM)
            verdict = str(d.get("verdict", "")).strip().lower()
            if verdict in ("valid", "invalid"):
                ballots.append({"verdict": verdict, "introduced_by_diff": bool(d.get("introduced_by_diff")),
                                "reason": str(d.get("reason", ""))[:300]})
        except Exception as e:
            print(f"    phiếu lỗi: {e}", flush=True)
    if not ballots:
        return {"verdict": "unknown", "introduced_by_diff": None, "ballots": []}
    tally = Counter(b["verdict"] for b in ballots)
    verdict = "valid" if tally["valid"] > tally["invalid"] else "invalid" if tally["invalid"] > tally["valid"] else "unknown"
    intro = sum(b["introduced_by_diff"] for b in ballots) * 2 > len(ballots)
    return {"verdict": verdict, "introduced_by_diff": intro, "ballots": ballots}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--truth", default=os.path.join("eval", "ground_truth.json"))
    ap.add_argument("--out", default=os.path.join("eval", "out"))
    ap.add_argument("--config", default="both")
    ap.add_argument("--votes", type=int, default=3)
    ap.add_argument("--tolerance", type=int, default=3)
    a = ap.parse_args()

    from app.config import settings
    from app.review.agent import get_backend

    gt = load_json(a.truth, None)
    if gt is None:
        raise SystemExit("Không đọc được ground truth")
    repo = gt["repo"]
    out_path = os.path.join(a.out, f"fp_judgement_{a.config}.json")
    done = {(r["case"], r["key"]): r for r in load_json(out_path, [])}
    backend = get_backend()
    judge_model = settings.anthropic_model if settings.agent_backend == "anthropic" else settings.llm_model

    records, tp_total = [], 0
    for cid, c in gt["cases"].items():
        findings = load_json(os.path.join(a.out, a.config, f"{cid}.json"), [])
        used = matched_indices(findings, c["truth"], a.tolerance)
        tp_total += len(used)
        for i, f in enumerate(findings):
            if i in used:
                continue
            key = f"{f['file']}:{f['line']}:{f.get('source','')}:{f.get('title','')[:60]}"
            rec = done.get((cid, key))
            if rec is None or rec.get("verdict") == "unknown":
                print(f"[{cid}] {f.get('source')} {f['file']}:{f['line']} {f.get('title','')[:60]}", flush=True)
                res = judge_one(backend, repo, c["branch"], f, a.votes)
                rec = {"case": cid, "key": key, "source": f.get("source"), "role": f.get("role"),
                       "category": f.get("category"), "title": f.get("title"), "judge_model": judge_model, **res}
                done[(cid, key)] = rec
                with open(out_path, "w", encoding="utf-8") as fh:
                    json.dump(list(done.values()), fh, ensure_ascii=False, indent=1)
            records.append(rec)

    fp = len(records)
    v = Counter(r["verdict"] for r in records)
    pre_existing = sum(1 for r in records if r["verdict"] == "valid" and not r["introduced_by_diff"])
    by_src = Counter(("llm" if r["source"] == "llm" else "static", r["verdict"]) for r in records)
    total = tp_total + fp
    lines = [f"# Phân loại FP — config `{a.config}` ({a.out}), giám khảo: {judge_model}, {a.votes} phiếu/FP", "",
             f"- TP = {tp_total}, FP = {fp}",
             f"- FP đúng sự thật (valid): **{v['valid']}** — trong đó {pre_existing} là vấn đề có từ trước, không do diff gây ra",
             f"- FP sai sự thật (invalid): **{v['invalid']}**" + (f" · không kết luận: {v['unknown']}" if v["unknown"] else ""),
             f"- Precision theo ground truth: {tp_total / total:.1%}" if total else "- (không có finding)",
             f"- Precision nếu tính cả nhận xét đúng ngoài ground truth: **{(tp_total + v['valid']) / total:.1%}**" if total else "",
             f"- Tỷ lệ nhận xét sai sự thật trên tổng số nhận xét: **{v['invalid'] / total:.1%}**" if total else "",
             "", "| Nguồn | valid | invalid | unknown |", "|---|---|---|---|"]
    for src in ("static", "llm"):
        lines.append(f"| {src} | {by_src[(src, 'valid')]} | {by_src[(src, 'invalid')]} | {by_src[(src, 'unknown')]} |")
    bad = [r for r in records if r["verdict"] == "invalid"]
    if bad:
        lines += ["", "## Các nhận xét bị giám khảo cho là sai", ""]
        lines += [f"- `{r['case']}` [{r['source']}] {r['title']} — {r['ballots'][0]['reason'] if r['ballots'] else ''}" for r in bad]
    md = os.path.join(a.out, f"fp_judgement_{a.config}.md")
    with open(md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
