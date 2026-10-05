"""Thử nghiệm trên PR THẬT: chạy HermesQA trên tập con Python của AACR-Bench (Alibaba, Apache-2.0).

Hai bộ dữ liệu sẵn có của đồ án đều là lỗi TIÊM nhân tạo. Script này thêm một bộ thứ ba: pull request
thật từ dự án mã nguồn mở, với danh sách vấn đề đã được kỹ sư xác nhận (label = 1 trong AACR-Bench).
Quy mô nhỏ (mặc định: các PR Python <= 210 dòng thay đổi) nên đây là THỬ NGHIỆM SƠ BỘ, không phải
benchmark đầy đủ; mục đích là đặt số liệu lỗi-tiêm cạnh một con số trên dữ liệu thật.

Cách chấm (phỏng theo bài báo AACR-Bench, arXiv 2601.19494):
  - "trúng vị trí": cùng file và dòng của finding nằm trong [from_line - 3, to_line + 3];
  - "trúng ngữ nghĩa": giám khảo LLM bỏ phiếu (đa số) rằng finding và nhận xét chuẩn nói về CÙNG một vấn đề.
  Báo cáo hai mức: chặt (vị trí + ngữ nghĩa) và lỏng (cùng file + ngữ nghĩa).
GIỚI HẠN: ground truth của PR thật không bao giờ đầy đủ, nên precision ở đây là cận dưới; giám khảo
là cùng model với bên sinh; cỡ mẫu nhỏ.

Dùng:
  python eval/aacr_eval.py --steps prepare             # tải dataset + checkout repo ở đúng commit (chỉ git)
  python eval/aacr_eval.py --steps run --configs llm,both
  python eval/aacr_eval.py --steps score --configs llm,both
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter, OrderedDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("GITHUB_APP_ID", "1")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "x")

DATASET_URL = "https://huggingface.co/datasets/Alibaba-Aone/aacr-bench/resolve/main/dataset.json"
WORK = os.path.join(ROOT, "eval", "aacr")
CONFIG_FLAGS = {"llm": ["--skip-static"], "both": [], "llm_noverify": ["--skip-static", "--no-verify"]}
TOKEN_RE = re.compile(r"tokens in=(\d+) out=(\d+)")
LINE_SLACK = 3

SAME_SYSTEM = "Bạn là kỹ sư phần mềm cấp cao, làm giám khảo so khớp nhận xét review code. Bạn chặt chẽ và công tâm."
SAME_PROMPT = """Một nhận xét CHUẨN (đã được kỹ sư xác nhận) và danh sách nhận xét do công cụ tự động đưa ra trên cùng một file.
Hãy cho biết nhận xét nào của công cụ nói về CÙNG MỘT VẤN ĐỀ với nhận xét chuẩn: cùng ý định cốt lõi và cùng
nội dung kỹ thuật (cùng đoạn code, cùng loại lỗi). Diễn đạt khác nhau không sao; cùng file nhưng vấn đề khác thì KHÔNG tính.

# Nhận xét chuẩn
File: <<PATH>>, dòng <<FROM>>-<<TO>>
<<NOTE>>

# Nhận xét của công cụ
<<CANDIDATES>>

Trả về DUY NHẤT một JSON object: {"match": <index của nhận xét trùng vấn đề, hoặc -1 nếu không có>, "reason": "một câu"}
"""


def sh(args, cwd=None, check=True):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:6])}...: {p.stderr.strip()[-400:]}")
    return p.stdout


def load_prs(lang: str, max_lines: int) -> "OrderedDict[str, dict]":
    os.makedirs(WORK, exist_ok=True)
    path = os.path.join(WORK, "dataset.json")
    if not os.path.exists(path):
        print("tải dataset AACR-Bench ...", flush=True)
        urllib.request.urlretrieve(DATASET_URL, path)
    rows = json.load(open(path, encoding="utf-8"))
    prs: "OrderedDict[str, dict]" = OrderedDict()
    for r in rows:
        if r["project_main_language"] != lang or str(r["label"]) != "1":
            continue
        if int(r["pr_change_line_count"]) > max_lines:
            continue
        m = re.match(r"https://github.com/([^/]+)/([^/]+)/pull/(\d+)", r["pr_url"])
        if not m:
            continue
        pid = f"{m.group(1)}_{m.group(2)}_{m.group(3)}"
        pr = prs.setdefault(pid, {"id": pid, "owner": m.group(1), "repo": m.group(2), "number": int(m.group(3)),
                                  "url": r["pr_url"], "source": r["pr_source_commit"], "target": r["pr_target_commit"],
                                  "lines": int(r["pr_change_line_count"]), "truth": []})
        pr["truth"].append({"path": r["path"], "from": int(r["from_line"] or 0), "to": int(r["to_line"] or 0),
                            "side": r.get("side", "right"), "note": r["note"], "category": r["category"],
                            "context": r["context"]})
    return prs


def repo_dir(pr: dict) -> str:
    return os.path.join(WORK, "repos", f"{pr['owner']}_{pr['repo']}")


def prepare(pr: dict) -> str:
    """Checkout repo ở commit nguồn của PR và ghi diff (đích -> nguồn). Trả đường dẫn file diff."""
    d = repo_dir(pr)
    diff_path = os.path.join(WORK, "diffs", pr["id"] + ".diff")
    os.makedirs(os.path.dirname(diff_path), exist_ok=True)
    url = f"https://github.com/{pr['owner']}/{pr['repo']}.git"
    if not os.path.isdir(os.path.join(d, ".git")):
        os.makedirs(d, exist_ok=True)
        sh(["git", "init", "-q", d])
        sh(["git", "-C", d, "config", "core.autocrlf", "false"])
    for sha in (pr["target"], pr["source"]):
        if subprocess.run(["git", "-C", d, "cat-file", "-e", sha + "^{commit}"], capture_output=True).returncode != 0:
            sh(["git", "-C", d, "fetch", "-q", "--depth", "1", url, sha])
    sh(["git", "-C", d, "checkout", "-q", "-f", pr["source"]])
    diff = sh(["git", "-C", d, "-c", "core.quotepath=false", "diff", pr["target"], pr["source"]])
    with open(diff_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(diff)
    return diff_path


def run_config(pr: dict, config: str, diff_path: str) -> dict:
    out_dir = os.path.join(WORK, "out", config)
    os.makedirs(out_dir, exist_ok=True)
    out_json = os.path.join(out_dir, pr["id"] + ".json")
    cmd = [sys.executable, "-m", "app.cli", "--repo", repo_dir(pr), "--diff-file", diff_path,
           "--out", os.path.join(out_dir, pr["id"] + ".md"), "--json", out_json,
           "--meta", os.path.join(out_dir, "_meta", pr["id"] + ".json"), *CONFIG_FLAGS[config]]
    env = dict(os.environ)
    env.update({"MIN_SEVERITY_TO_COMMENT": "low", "MIN_CONFIDENCE": "0.0", "PYTHONIOENCODING": "utf-8"})
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=1800)
        code, err = p.returncode, p.stderr
    except subprocess.TimeoutExpired:
        code, err = -1, "TIMEOUT"
    rec = {"case": pr["id"], "config": config, "seconds": round(time.time() - t0, 1), "returncode": code,
           "tokens_in": sum(int(m.group(1)) for m in TOKEN_RE.finditer(err)),
           "tokens_out": sum(int(m.group(2)) for m in TOKEN_RE.finditer(err))}
    if code != 0:
        rec["error"] = err[-600:]
        if not os.path.exists(out_json):
            json.dump([], open(out_json, "w", encoding="utf-8"))
    return rec


def location_ok(f: dict, t: dict) -> bool:
    if f["file"] != t["path"]:
        return False
    if t["side"] != "right" or not t["from"]:
        return True                      # nhận xét trên dòng bị xoá: không so được số dòng phía mới
    return t["from"] - LINE_SLACK <= int(f["line"]) <= max(t["to"], t["from"]) + LINE_SLACK


def judge_match(backend, t: dict, cands: list[tuple[int, dict]], votes: int) -> int:
    """Trả chỉ số (trong danh sách findings của PR) của finding cùng vấn đề với t, hoặc -1."""
    listing = "\n".join(f"[{k}] dòng {f['line']} · {f.get('category')} · {f.get('title')}\n    {f.get('explanation','')[:500]}"
                        for k, (_, f) in enumerate(cands))
    prompt = (SAME_PROMPT.replace("<<PATH>>", t["path"]).replace("<<FROM>>", str(t["from"])).replace("<<TO>>", str(t["to"]))
              .replace("<<NOTE>>", t["note"][:1500]).replace("<<CANDIDATES>>", listing))
    ballots = []
    for _ in range(votes):
        try:
            d = backend.run_json("SE", prompt, system=SAME_SYSTEM)
            m = d.get("match", -1)
            ballots.append(m if isinstance(m, int) and not isinstance(m, bool) and 0 <= m < len(cands) else -1)
        except Exception as e:
            print(f"    phiếu lỗi: {e}", flush=True)
    if not ballots:
        return -1
    winner, n = Counter(ballots).most_common(1)[0]
    return cands[winner][0] if winner >= 0 and n * 2 > len(ballots) else -1


def score_config(prs, config: str, votes: int) -> dict:
    from app.review.agent import get_backend
    backend = get_backend()
    out_dir = os.path.join(WORK, "out", config)
    cache_path = os.path.join(out_dir, "_judge_cache.json")
    cache = json.load(open(cache_path, encoding="utf-8")) if os.path.exists(cache_path) else {}
    tot = Counter()
    by_ctx = Counter()
    rows = []
    for pr in prs.values():
        fpath = os.path.join(out_dir, pr["id"] + ".json")
        if not os.path.exists(fpath):
            continue
        findings = json.load(open(fpath, encoding="utf-8"))
        used_strict: set[int] = set()
        used_loose: set[int] = set()
        tp_s = tp_l = 0
        for ti, t in enumerate(pr["truth"]):
            by_ctx[(t["context"], "total")] += 1
            cands = [(i, f) for i, f in enumerate(findings) if f["file"] == t["path"]]
            if not cands:
                continue
            key = f"{pr['id']}|{ti}|{len(findings)}"
            if key not in cache:
                print(f"[{config}] {pr['id']} truth#{ti} ({len(cands)} ứng viên)", flush=True)
                cache[key] = judge_match(backend, t, cands, votes)
                json.dump(cache, open(cache_path, "w", encoding="utf-8"))
            hit = cache[key]
            if hit < 0:
                continue
            if hit not in used_loose:
                used_loose.add(hit); tp_l += 1
                by_ctx[(t["context"], "loose")] += 1
            if location_ok(findings[hit], t) and hit not in used_strict:
                used_strict.add(hit); tp_s += 1
                by_ctx[(t["context"], "strict")] += 1
        n_f, n_t = len(findings), len(pr["truth"])
        tot.update(findings=n_f, truth=n_t, strict=tp_s, loose=tp_l)
        rows.append((pr["id"], pr["lines"], n_t, n_f, tp_s, tp_l))
    return {"tot": tot, "rows": rows, "by_ctx": by_ctx}


def prf(tp: int, n_found: int, n_truth: int):
    p = tp / n_found if n_found else 0.0
    r = tp / n_truth if n_truth else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="prepare,run,score")
    ap.add_argument("--configs", default="llm")
    ap.add_argument("--lang", default="Python")
    ap.add_argument("--max-lines", type=int, default=210)
    ap.add_argument("--votes", type=int, default=3)
    a = ap.parse_args()
    steps = a.steps.split(",")
    configs = [c for c in a.configs.split(",") if c]
    prs = load_prs(a.lang, a.max_lines)
    print(f"{len(prs)} PR {a.lang} (<= {a.max_lines} dòng), {sum(len(p['truth']) for p in prs.values())} vấn đề chuẩn", flush=True)

    diffs = {}
    if "prepare" in steps or "run" in steps:
        for pr in list(prs.values()):
            try:
                diffs[pr["id"]] = prepare(pr)
                print(f"  sẵn sàng {pr['id']} ({pr['lines']} dòng)", flush=True)
            except Exception as e:
                print(f"  BỎ {pr['id']}: không lấy được commit ({e})", flush=True)
                del prs[pr["id"]]

    if "run" in steps:
        for cfg in configs:
            runs_path = os.path.join(WORK, "out", f"runs_{cfg}.json")
            os.makedirs(os.path.dirname(runs_path), exist_ok=True)
            runs = {r["case"]: r for r in (json.load(open(runs_path, encoding="utf-8")) if os.path.exists(runs_path) else [])}
            for pr in prs.values():
                if runs.get(pr["id"], {}).get("returncode") == 0:
                    continue
                # mỗi PR cần working tree ở đúng commit nguồn (context file đọc từ đó)
                sh(["git", "-C", repo_dir(pr), "checkout", "-q", "-f", pr["source"]])
                print(f"[{cfg}] {pr['id']} ...", flush=True)
                rec = run_config(pr, cfg, diffs[pr["id"]])
                print(f"    -> {'OK' if rec['returncode'] == 0 else 'LỖI'} {rec['seconds']}s tokens {rec['tokens_in']}/{rec['tokens_out']}", flush=True)
                runs[pr["id"]] = rec
                json.dump(list(runs.values()), open(runs_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    if "score" in steps:
        lines = [f"# HermesQA trên PR thật — AACR-Bench, tập con {a.lang} ({len(prs)} PR <= {a.max_lines} dòng)", "",
                 "Chặt = cùng file, dòng trong khoảng ±3, và giám khảo LLM xác nhận cùng vấn đề. "
                 "Lỏng = cùng file và cùng vấn đề (bỏ qua số dòng).", "",
                 "| Config | PR | Vấn đề chuẩn | Finding | TP chặt | P | R | F1 | TP lỏng | P | R | F1 |",
                 "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        detail = []
        for cfg in configs:
            res = score_config(prs, cfg, a.votes)
            t = res["tot"]
            ps, rs, fs = prf(t["strict"], t["findings"], t["truth"])
            pl, rl, fl = prf(t["loose"], t["findings"], t["truth"])
            lines.append(f"| {cfg} | {len(res['rows'])} | {t['truth']} | {t['findings']} | {t['strict']} | {ps:.1%} | {rs:.1%} | {fs:.1%} "
                         f"| {t['loose']} | {pl:.1%} | {rl:.1%} | {fl:.1%} |")
            detail += ["", f"## `{cfg}` — theo mức ngữ cảnh cần có để thấy vấn đề", "",
                       "| Mức ngữ cảnh | Tổng | Bắt được (chặt) | Bắt được (lỏng) |", "|---|---|---|---|"]
            for ctx in ("Diff Level", "File Level", "Repo Level"):
                bc = res["by_ctx"]
                detail.append(f"| {ctx} | {bc[(ctx, 'total')]} | {bc[(ctx, 'strict')]} | {bc[(ctx, 'loose')]} |")
            detail += ["", "| PR | Dòng đổi | Vấn đề chuẩn | Finding | TP chặt | TP lỏng |", "|---|---|---|---|---|---|"]
            detail += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} |" for r in res["rows"]]
        out_md = os.path.join(WORK, "out", "results.md")
        with open(out_md, "w", encoding="utf-8") as f:
            f.write("\n".join(lines + detail) + "\n")
        print("\n".join(lines + detail))
        print(f"\nChi tiết: {out_md}")


if __name__ == "__main__":
    main()
