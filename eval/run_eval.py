"""Chạy eval: với mỗi case trong ground_truth.json, checkout branch tương ứng rồi chạy
app.cli theo từng cấu hình, lưu findings JSON + thời gian + token.

Cấu hình:
  static = chỉ static tools (--roles "")        -> miễn phí, cần Docker
  llm    = chỉ LLM (--skip-static)              -> tốn token
  both   = static + LLM (mặc định của HermesQA) -> tốn token, cần Docker

Dùng:
  python eval/run_eval.py --configs static            # toàn bộ case, chỉ static
  python eval/run_eval.py --configs llm,both --cases sqli-concat,secret-aws
  python eval/run_eval.py --configs llm,both --limit 5
Kết quả: eval/out/<config>/<case>.json + eval/out/runs_<config>.json
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_FLAGS = {
    "static": ["--roles", ""],
    "llm": ["--skip-static"],
    "both": [],
}

TOKEN_RE = re.compile(r"tokens in=(\d+) out=(\d+)")


def run_case(repo: str, branch: str, cid: str, config: str, out_dir: str) -> dict:
    subprocess.run(["git", "-C", repo, "checkout", "-q", branch], check=True)
    out_json = os.path.join(out_dir, f"{cid}.json")
    out_md = os.path.join(out_dir, f"{cid}.md")
    cmd = [sys.executable, "-m", "app.cli", "--repo", repo, "--base", "main", "--head", branch,
           "--out", out_md, "--json", out_json, *CONFIG_FLAGS[config]]

    env = dict(os.environ)
    # Eval đo KHẢ NĂNG PHÁT HIỆN nên nới policy: giữ mọi severity, không lọc confidence.
    env.update({"MIN_SEVERITY_TO_COMMENT": "low", "MIN_CONFIDENCE": "0.0",
                "PYTHONIOENCODING": "utf-8"})

    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=1800)
        stderr, stdout, code = proc.stderr, proc.stdout, proc.returncode
    except subprocess.TimeoutExpired as e:
        # 1 case treo (VD Docker daemon tắt giữa chừng) không được làm chết cả lần chạy:
        # ghi nhận là lỗi rồi đi tiếp, lần chạy sau sẽ tự thử lại case này.
        def _txt(v):
            return v.decode("utf-8", "replace") if isinstance(v, bytes) else (v or "")
        stderr, stdout, code = _txt(e.stderr), _txt(e.stdout), -1
        stderr += "\nTIMEOUT: quá 1800s (kiểm tra Docker daemon còn chạy không)"
    elapsed = time.time() - t0

    tokens_in = sum(int(m.group(1)) for m in TOKEN_RE.finditer(stderr))
    tokens_out = sum(int(m.group(2)) for m in TOKEN_RE.finditer(stderr))

    rec = {"case": cid, "config": config, "seconds": round(elapsed, 1),
           "tokens_in": tokens_in, "tokens_out": tokens_out,
           "returncode": code}
    if code != 0:
        rec["error"] = (stderr or stdout)[-800:]
        # vẫn ghi findings rỗng để score.py tính là miss thay vì bỏ case
        if not os.path.exists(out_json):
            with open(out_json, "w", encoding="utf-8") as f:
                json.dump([], f)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--truth", default=os.path.join("eval", "ground_truth.json"))
    ap.add_argument("--configs", default="static", help="VD: static,llm,both")
    ap.add_argument("--cases", help="chỉ chạy các case này (phân tách bằng dấu phẩy)")
    ap.add_argument("--limit", type=int, help="chỉ chạy N case đầu")
    ap.add_argument("--out", default=os.path.join("eval", "out"))
    a = ap.parse_args()

    with open(a.truth, encoding="utf-8") as f:
        gt = json.load(f)
    repo = gt["repo"]
    cases = list(gt["cases"].items())
    if a.cases:
        want = set(a.cases.split(","))
        cases = [(cid, c) for cid, c in cases if cid in want]
    if a.limit:
        cases = cases[: a.limit]

    configs = [c.strip() for c in a.configs.split(",") if c.strip()]
    for cfg in configs:
        if cfg not in CONFIG_FLAGS:
            raise SystemExit(f"config không hợp lệ: {cfg} (chọn static|llm|both)")

    for cfg in configs:
        out_dir = os.path.join(a.out, cfg)
        os.makedirs(out_dir, exist_ok=True)
        runs_path = os.path.join(a.out, f"runs_{cfg}.json")
        runs = {}
        if os.path.exists(runs_path):  # cho phép chạy nối tiếp/resume
            with open(runs_path, encoding="utf-8") as f:
                runs = {r["case"]: r for r in json.load(f)}

        for i, (cid, c) in enumerate(cases, 1):
            done = runs.get(cid)
            if done and done.get("returncode") == 0:
                print(f"[{cfg} {i}/{len(cases)}] {cid}: đã có, bỏ qua")
                continue
            print(f"[{cfg} {i}/{len(cases)}] {cid} ...", flush=True)
            rec = run_case(repo, c["branch"], cid, cfg, out_dir)
            status = "OK" if rec["returncode"] == 0 else "LỖI"
            print(f"    -> {status} {rec['seconds']}s, tokens {rec['tokens_in']}/{rec['tokens_out']}", flush=True)
            runs[cid] = rec
            with open(runs_path, "w", encoding="utf-8") as f:
                json.dump(list(runs.values()), f, ensure_ascii=False, indent=1)

    subprocess.run(["git", "-C", repo, "checkout", "-q", "main"], check=True)
    print("Xong. Chấm điểm: python eval/score.py")


if __name__ == "__main__":
    main()
