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
    # Ablation: LLM nhung TAT buoc self-verify. So voi "llm" de do xem buoc tu
    # kiem chung co that su loc bo finding sai hay khong. Self-verify hien dung
    # CHINH model va CHINH vai da sinh ra finding, nen can do chu khong nen mac
    # dinh tin la no hoat dong.
    "llm_noverify": ["--skip-static", "--no-verify"],
    # Doi chung: buoc kiem chung kieu CU ("chi giu finding that su la van de") tren cung pipeline moi,
    # de so 3 che do tren cung mot nen: factcheck (mac dinh, config "llm") / legacy / off.
    "llm_legacyverify": ["--skip-static", "--verify", "legacy"],
}

TOKEN_RE = re.compile(r"tokens in=(\d+) out=(\d+)")


def eval_env(profile: str | None = None) -> dict:
    """Môi trường cho app.cli khi chạy eval. `profile` = tên profile LLM trong .env (LLM_<TÊN>_MODEL...),
    truyền qua LLM_PROFILE để cùng một lệnh đo được nhiều model (Gemini, Muse Spark...) không phải sửa .env."""
    env = dict(os.environ)
    # Eval đo KHẢ NĂNG PHÁT HIỆN nên nới policy: giữ mọi severity, không lọc confidence.
    env.update({"MIN_SEVERITY_TO_COMMENT": "low", "MIN_CONFIDENCE": "0.0",
                "PYTHONIOENCODING": "utf-8"})
    if profile:
        env["LLM_PROFILE"] = profile
    return env


def resolve_model(profile: str | None) -> str:
    """Hỏi app.config xem profile này ứng với model nào (lỗi sớm nếu .env thiếu LLM_<TÊN>_MODEL)."""
    code = ("from app.config import settings; "
            "print(settings.anthropic_model if settings.agent_backend == 'anthropic' else settings.llm_model)")
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=eval_env(profile), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SystemExit(f"không nạp được cấu hình LLM (profile={profile or '-'}):\n{proc.stderr.strip()[-600:]}")
    return proc.stdout.strip()


def run_case(repo: str, branch: str, cid: str, config: str, out_dir: str, profile: str | None = None) -> dict:
    subprocess.run(["git", "-C", repo, "checkout", "-q", branch], check=True)
    out_json = os.path.join(out_dir, f"{cid}.json")
    out_md = os.path.join(out_dir, f"{cid}.md")
    cmd = [sys.executable, "-m", "app.cli", "--repo", repo, "--base", "main", "--head", branch,
           "--out", out_md, "--json", out_json,
           "--run-source", "eval", "--meta", os.path.join(out_dir, "_meta", f"{cid}.json"), *CONFIG_FLAGS[config]]
    env = eval_env(profile)

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
    if profile:
        rec["profile"] = profile
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
    ap.add_argument("--profile", help="profile LLM trong .env (LLM_<TÊN>_MODEL...), VD: gemini | muse. "
                                      "Nhớ đổi --out (VD eval/out-muse) để không trộn kết quả hai model")
    a = ap.parse_args()

    model = resolve_model(a.profile)
    print(f"model: {model}" + (f" (profile {a.profile})" if a.profile else ""))
    if a.profile and os.path.normpath(a.out) == os.path.normpath(os.path.join("eval", "out")):
        raise SystemExit("--profile cần đi kèm --out riêng (VD --out eval/out-muse) để không trộn với kết quả model mặc định")

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
            raise SystemExit(f"config không hợp lệ: {cfg} (chọn {"|".join(CONFIG_FLAGS)})")

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
            rec = run_case(repo, c["branch"], cid, cfg, out_dir, a.profile)
            status = "OK" if rec["returncode"] == 0 else "LỖI"
            print(f"    -> {status} {rec['seconds']}s, tokens {rec['tokens_in']}/{rec['tokens_out']}", flush=True)
            runs[cid] = rec
            with open(runs_path, "w", encoding="utf-8") as f:
                json.dump(list(runs.values()), f, ensure_ascii=False, indent=1)

    subprocess.run(["git", "-C", repo, "checkout", "-q", "main"], check=True)
    print("Xong. Chấm điểm: python eval/score.py")


if __name__ == "__main__":
    main()
