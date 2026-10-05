"""Chấm điểm eval: so findings của từng cấu hình với ground truth.

Một finding được tính ĐÚNG (true positive) nếu khớp một ground-truth chưa được khớp:
  - cùng file,
  - |line dự đoán - line thật| <= 3,
  - category nằm trong danh sách categories chấp nhận của ground truth.
Precision = TP / (TP + FP), Recall = TP / (TP + FN), F1 = trung bình điều hòa.

Dùng:  python eval/score.py            (tự phát hiện các config đã chạy trong eval/out)
Ghi:   eval/out/results.md
"""
import argparse
import json
import os
from collections import defaultdict

LINE_TOLERANCE = 3


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def match_case(findings: list[dict], truths: list[dict]):
    """Greedy match: mỗi truth khớp tối đa 1 finding và ngược lại. Trả (tp, fp, fn, missed)."""
    used = set()
    matched_truths = set()
    for t_idx, t in enumerate(truths):
        best = None
        for f_idx, f in enumerate(findings):
            if f_idx in used:
                continue
            if f["file"] != t["file"]:
                continue
            if abs(int(f["line"]) - int(t["line"])) > LINE_TOLERANCE:
                continue
            if f.get("category") not in t["categories"]:
                continue
            d = abs(int(f["line"]) - int(t["line"]))
            if best is None or d < best[0]:
                best = (d, f_idx)
        if best is not None:
            used.add(best[1])
            matched_truths.add(t_idx)
    tp = len(matched_truths)
    fp = len(findings) - len(used)
    fn = len(truths) - tp
    missed = [t for i, t in enumerate(truths) if i not in matched_truths]
    return tp, fp, fn, missed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--truth", default=os.path.join("eval", "ground_truth.json"))
    ap.add_argument("--out", default=os.path.join("eval", "out"))
    a = ap.parse_args()

    gt = load_json(a.truth, None)
    if gt is None:
        raise SystemExit("Chưa có ground_truth.json — chạy make_dataset.py trước")

    # Tu phat hien moi config da chay (static, llm, both, llm_noverify, ...):
    # moi thu muc con co file findings .json la mot config.
    known = ["static", "llm", "both", "llm_noverify"]
    found = sorted(d for d in os.listdir(a.out) if os.path.isdir(os.path.join(a.out, d))
                   and any(n.endswith(".json") for n in os.listdir(os.path.join(a.out, d))))
    configs = [d for d in known if d in found] + [d for d in found if d not in known]
    if not configs:
        raise SystemExit("Chưa có kết quả nào trong eval/out — chạy run_eval.py trước")

    lines = ["# Kết quả eval HermesQA", ""]
    summary_rows = []

    for cfg in configs:
        runs = {r["case"]: r for r in load_json(os.path.join(a.out, f"runs_{cfg}.json"), [])}
        total_tp = total_fp = total_fn = 0
        cat_stats = defaultdict(lambda: [0, 0])   # category chính -> [bắt được, tổng]
        missed_all = []
        n_cases = 0

        for cid, c in gt["cases"].items():
            fpath = os.path.join(a.out, cfg, f"{cid}.json")
            if not os.path.exists(fpath) and cid not in runs:
                continue  # case chưa chạy với config này -> loại khỏi thống kê
            n_cases += 1
            findings = load_json(fpath, [])
            tp, fp, fn, missed = match_case(findings, c["truth"])
            total_tp += tp; total_fp += fp; total_fn += fn
            missed_ids = [id(m) for m in missed]
            for t in c["truth"]:
                cat = t["categories"][0]
                cat_stats[cat][1] += 1
                if id(t) not in missed_ids:
                    cat_stats[cat][0] += 1
            for t in missed:
                missed_all.append((cid, t))

        prec = total_tp / (total_tp + total_fp) if total_tp + total_fp else 0.0
        rec = total_tp / (total_tp + total_fn) if total_tp + total_fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        ok_runs = [r for r in runs.values() if r.get("returncode") == 0]
        avg_s = sum(r["seconds"] for r in ok_runs) / len(ok_runs) if ok_runs else 0
        avg_tok = sum(r["tokens_in"] + r["tokens_out"] for r in ok_runs) / len(ok_runs) if ok_runs else 0
        summary_rows.append((cfg, n_cases, total_tp, total_fp, total_fn, prec, rec, f1, avg_s, avg_tok))

        lines += [f"## Cấu hình `{cfg}` ({n_cases} case)", "",
                  f"- TP={total_tp} FP={total_fp} FN={total_fn}",
                  f"- **Precision {prec:.1%} · Recall {rec:.1%} · F1 {f1:.1%}**",
                  f"- Trung bình: {avg_s:.1f}s/PR · {avg_tok:.0f} tokens/PR", "",
                  "| Category | Bắt được / Tổng |", "|---|---|"]
        for cat, (tp_c, tot) in sorted(cat_stats.items()):
            lines.append(f"| {cat} | {tp_c}/{tot} |")
        if missed_all:
            lines += ["", "<details><summary>Các lỗi bị bỏ sót</summary>", ""]
            lines += [f"- `{cid}` — {t['file']}:{t['line']} ({t['categories'][0]}): {t['note']}"
                      for cid, t in missed_all]
            lines += ["", "</details>"]
        lines.append("")

    lines += ["## Tổng hợp", "",
              "| Config | Cases | TP | FP | FN | Precision | Recall | F1 | s/PR | tokens/PR |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for cfg, n, tp, fp, fn, p, r, f1, s, tok in summary_rows:
        lines.append(f"| {cfg} | {n} | {tp} | {fp} | {fn} | {p:.1%} | {r:.1%} | {f1:.1%} | {s:.1f} | {tok:.0f} |")

    out_md = os.path.join(a.out, "results.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("\n".join(lines[lines.index("## Tổng hợp"):]))
    print(f"\nChi tiết: {out_md}")


if __name__ == "__main__":
    main()
