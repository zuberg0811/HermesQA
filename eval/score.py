"""Chấm điểm kết quả eval: khớp finding với ground truth, in bảng tổng hợp, ghi <out>/results.md.

Một finding là true positive khi: cùng file, lệch dòng <= tolerance, category nằm trong danh sách chấp nhận,
và ground truth đó chưa được finding nào khác khớp (mỗi đáp án chỉ khớp một lần -> báo trùng tính là FP).

Ngoài điểm theo cấu hình (static / llm / both ...), còn tính điểm THEO VAI (SE, QA, DevOps, và từng công cụ
static) để dashboard cho thấy agent nào đóng góp gì: TP của vai = finding của vai đó được khớp; FP của vai =
finding của vai đó không khớp. Recall theo vai không có nghĩa (một đáp án không "thuộc" vai nào) nên chỉ
báo precision và số TP.

Dùng:
  python eval/score.py                         # eval/out, tolerance 3 -> results.md
  python eval/score.py --out eval/out-v3 --tolerance 0        # -> results_tol0.md
Hàm `score_dir()` được dashboard (app/web/data.py) gọi trực tiếp.
"""
import argparse
import json
import os
from collections import defaultdict

LINE_TOLERANCE = 3
KNOWN_ORDER = ["static", "llm", "both", "llm_noverify", "llm_legacyverify"]


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def matched_indices(findings: list[dict], truths: list[dict], tol: int = LINE_TOLERANCE) -> dict[int, int]:
    """Greedy match: moi truth khop toi da 1 finding va nguoc lai. Tra {chi so finding: chi so truth}."""
    used: dict[int, int] = {}
    for t_idx, t in enumerate(truths):
        best = None
        for f_idx, f in enumerate(findings):
            if f_idx in used:
                continue
            if f["file"] != t["file"]:
                continue
            if abs(int(f["line"]) - int(t["line"])) > tol:
                continue
            if f.get("category") not in t["categories"]:
                continue
            d = abs(int(f["line"]) - int(t["line"]))
            if best is None or d < best[0]:
                best = (d, f_idx)
        if best is not None:
            used[best[1]] = t_idx
    return used


def match_case(findings: list[dict], truths: list[dict], tol: int = LINE_TOLERANCE):
    """Tra (tp, fp, fn, missed)."""
    used = matched_indices(findings, truths, tol)
    matched_truths = set(used.values())
    tp = len(matched_truths)
    fp = len(findings) - len(used)
    fn = len(truths) - tp
    missed = [t for i, t in enumerate(truths) if i not in matched_truths]
    return tp, fp, fn, missed


def agent_of(f: dict) -> str:
    """Nhãn 'agent' của một finding: vai LLM (SE/QA/DevOps) hoặc tên công cụ static."""
    return (f.get("role") or "llm") if f.get("source") == "llm" else (f.get("source") or "static")


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def list_configs(out_dir: str) -> list[str]:
    """Mọi thư mục con có file findings .json là một config; thứ tự quen thuộc trước."""
    if not os.path.isdir(out_dir):
        return []
    found = sorted(d for d in os.listdir(out_dir) if os.path.isdir(os.path.join(out_dir, d))
                   and not d.startswith("_")
                   and any(n.endswith(".json") for n in os.listdir(os.path.join(out_dir, d))))
    return [d for d in KNOWN_ORDER if d in found] + [d for d in found if d not in KNOWN_ORDER]


def score_dir(gt: dict, out_dir: str, tol: int = LINE_TOLERANCE, configs: list[str] | None = None) -> dict:
    """Chấm mọi config trong out_dir. Trả dict thuần (JSON được) để CLI in và dashboard hiển thị:
    {"tolerance", "configs": {cfg: {"cases", "tp", "fp", "fn", "precision", "recall", "f1", "avg_seconds",
                                    "avg_tokens", "by_category": {cat: [hit, total]},
                                    "by_agent": {agent: {"tp", "fp", "precision", "findings"}},
                                    "missed": [{"case", "file", "line", "category", "note"}]}}}
    """
    configs = configs or list_configs(out_dir)
    result = {"tolerance": tol, "out_dir": out_dir, "configs": {}}
    for cfg in configs:
        runs = {r["case"]: r for r in load_json(os.path.join(out_dir, f"runs_{cfg}.json"), [])}
        tot_tp = tot_fp = tot_fn = 0
        by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        by_agent: dict[str, dict] = defaultdict(lambda: {"tp": 0, "fp": 0})
        missed_all = []
        n_cases = 0
        for cid, c in gt["cases"].items():
            fpath = os.path.join(out_dir, cfg, f"{cid}.json")
            if not os.path.exists(fpath) and cid not in runs:
                continue
            n_cases += 1
            findings = load_json(fpath, [])
            used = matched_indices(findings, c["truth"], tol)
            matched_truths = set(used.values())
            tp = len(matched_truths)
            tot_tp += tp; tot_fp += len(findings) - len(used); tot_fn += len(c["truth"]) - tp
            for i, t in enumerate(c["truth"]):
                by_cat[t["categories"][0]][1] += 1
                if i in matched_truths:
                    by_cat[t["categories"][0]][0] += 1
                else:
                    missed_all.append({"case": cid, "file": t["file"], "line": t["line"],
                                       "category": t["categories"][0], "note": t.get("note", "")})
            for i, f in enumerate(findings):
                by_agent[agent_of(f)]["tp" if i in used else "fp"] += 1
        p, r, f1 = prf(tot_tp, tot_fp, tot_fn)
        ok = [x for x in runs.values() if x.get("returncode") == 0]
        for a in by_agent.values():
            a["findings"] = a["tp"] + a["fp"]
            a["precision"] = a["tp"] / a["findings"] if a["findings"] else 0.0
        result["configs"][cfg] = {
            "cases": n_cases, "tp": tot_tp, "fp": tot_fp, "fn": tot_fn,
            "precision": p, "recall": r, "f1": f1,
            "avg_seconds": sum(x["seconds"] for x in ok) / len(ok) if ok else 0.0,
            "avg_tokens": sum(x["tokens_in"] + x["tokens_out"] for x in ok) / len(ok) if ok else 0.0,
            "failed_runs": len(runs) - len(ok),
            "by_category": dict(by_cat),
            "by_agent": dict(sorted(by_agent.items())),
            "missed": missed_all,
        }
    return result


def render_markdown(res: dict) -> str:
    lines = ["# Kết quả eval HermesQA", ""]
    if res["tolerance"] != LINE_TOLERANCE:
        lines += [f"Độ lệch dòng tối đa: {res['tolerance']}", ""]
    for cfg, s in res["configs"].items():
        lines += [f"## Cấu hình `{cfg}` ({s['cases']} case)", "",
                  f"- TP={s['tp']} FP={s['fp']} FN={s['fn']}",
                  f"- **Precision {s['precision']:.1%} · Recall {s['recall']:.1%} · F1 {s['f1']:.1%}**",
                  f"- Trung bình: {s['avg_seconds']:.1f}s/PR · {s['avg_tokens']:.0f} tokens/PR", "",
                  "| Category | Bắt được / Tổng |", "|---|---|"]
        lines += [f"| {cat} | {hit}/{tot} |" for cat, (hit, tot) in sorted(s["by_category"].items())]
        lines += ["", "| Agent / công cụ | Finding | TP | FP | Precision |", "|---|---|---|---|---|"]
        lines += [f"| {a} | {v['findings']} | {v['tp']} | {v['fp']} | {v['precision']:.1%} |"
                  for a, v in s["by_agent"].items()]
        if s["missed"]:
            lines += ["", "<details><summary>Các lỗi bị bỏ sót</summary>", ""]
            lines += [f"- `{m['case']}` — {m['file']}:{m['line']} ({m['category']}): {m['note']}" for m in s["missed"]]
            lines += ["", "</details>"]
        lines.append("")
    lines += ["## Tổng hợp", "",
              "| Config | Cases | TP | FP | FN | Precision | Recall | F1 | s/PR | tokens/PR |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for cfg, s in res["configs"].items():
        lines.append(f"| {cfg} | {s['cases']} | {s['tp']} | {s['fp']} | {s['fn']} | {s['precision']:.1%} "
                     f"| {s['recall']:.1%} | {s['f1']:.1%} | {s['avg_seconds']:.1f} | {s['avg_tokens']:.0f} |")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--truth", default=os.path.join("eval", "ground_truth.json"))
    ap.add_argument("--out", default=os.path.join("eval", "out"))
    ap.add_argument("--tolerance", type=int, default=LINE_TOLERANCE,
                    help="do lech dong toi da de tinh la trung (mac dinh 3; dat 0 de do do chinh xac cua viec ghim)")
    a = ap.parse_args()

    gt = load_json(a.truth, None)
    if gt is None:
        raise SystemExit("Chưa có ground_truth.json — chạy make_dataset.py trước")
    res = score_dir(gt, a.out, a.tolerance)
    if not res["configs"]:
        raise SystemExit(f"Chưa có kết quả nào trong {a.out} — chạy run_eval.py trước")

    md = render_markdown(res)
    out_md = os.path.join(a.out, "results.md" if a.tolerance == LINE_TOLERANCE else f"results_tol{a.tolerance}.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(md)
    print(md[md.index("## Tổng hợp"):])
    print(f"Chi tiết: {out_md}")


if __name__ == "__main__":
    main()
