"""CLI chạy review offline trên repo local — dùng để dev nhanh và chạy eval cho đồ án.
Dùng chung app.review.pipeline với worker, để hai đường chạy không lệch nhau.
Ví dụ:
  python -m app.cli --repo ./some-repo --base main --head feature-x --out report.md
  python -m app.cli --repo . --diff-file pr.diff --skip-static
"""
import argparse
import json
import logging
import os
import subprocess
import tempfile

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hermesqa.cli")

from app.config import settings
from app.review import postprocess, report, static_tools
from app.review.agent import get_backend
from app.review.diff_utils import parse_diff
from app.review.pipeline import LLMReview, attach_code_to_static, run_llm_review, scope_static_to_diff
from app.review.router import select_roles
from app.review.schemas import ReviewConfig
from app.review.static_tools import collect as collect_static
import yaml


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", default="main")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--diff-file", help="dùng diff sẵn thay vì git diff")
    ap.add_argument("--out", default="report.md")
    ap.add_argument("--json", help="ghi findings ra JSON (cho eval)")
    ap.add_argument("--meta", help="ghi thông tin phụ ra JSON: file bị bỏ qua, finding bị loại, thống kê ghim")
    ap.add_argument("--skip-static", action="store_true")
    ap.add_argument("--verify", choices=["factcheck", "legacy", "off"],
                    help="bước kiểm chứng sau khi LLM sinh finding (mặc định: VERIFY_MODE trong cấu hình)")
    ap.add_argument("--no-verify", action="store_true", help="tương đương --verify off")
    ap.add_argument("--roles", help="VD: SE,QA")
    a = ap.parse_args()

    if a.diff_file:
        diff = open(a.diff_file, encoding="utf-8").read()
    else:
        diff = subprocess.check_output(["git", "-C", a.repo, "diff", f"{a.base}...{a.head}"],
                                       text=True, encoding="utf-8", errors="replace")

    cfg_path = os.path.join(a.repo, ".hermesqa.yml")
    cfg = ReviewConfig.model_validate(yaml.safe_load(open(cfg_path, encoding="utf-8")) or {}) if os.path.exists(cfg_path) else ReviewConfig()
    files = [f for f in parse_diff(diff, cfg.ignore) if not f.is_binary and not f.is_deleted]
    if not files:
        print("Không có file để review"); return
    changed = {f.path for f in files}

    static = []
    tools = None
    if not a.skip_static:
        from app.sandbox import run_static_analysis   # import muộn: --skip-static không cần Docker SDK
        out_dir = tempfile.mkdtemp(prefix="hqa-out-")
        run_static_analysis(a.repo, out_dir, targets=sorted(changed))
        all_static = collect_static(out_dir)
        static = scope_static_to_diff([f for f in all_static if f.file in changed], files)
        tools = static_tools.tool_status(out_dir, all_static)
        for name, st in tools.items():
            if st["state"] == "failed":
                log.error("cong cu static THAT BAI: %s (ket qua thieu phan cua no)", name)
    attach_code_to_static(static, files)

    roles = [r for r in a.roles.split(",") if r.strip()] if a.roles is not None else select_roles(files, cfg.roles)
    pr_meta = {"pr_number": 0, "title": f"{a.base}..{a.head}", "body": "", "owner": "local", "repo": os.path.basename(os.path.abspath(a.repo)), "head_sha": a.head}
    mode = "off" if a.no_verify else (a.verify or settings.verify_mode)

    res = LLMReview()
    if roles:
        for role in roles:
            print(f"[agent] {role} ...")
        res = run_llm_review(get_backend(), roles, files, a.repo, pr_meta, static, cfg.conventions, "", mode)
        for role in res.failed_roles:
            print(f"[agent] {role} LỖI: {res.summaries.get(role)}")

    all_f = postprocess.dedupe(static + res.findings)
    inline, overflow = postprocess.apply_policy(all_f, files, settings.min_confidence, settings.min_severity_to_comment, cfg.max_comments or settings.max_comments_per_pr)
    final = inline + overflow
    for f in final:
        f.fingerprint = postprocess.fingerprint(f)
    md = report.build_markdown(pr_meta, roles, res.summaries, final, settings.llm_model, tools,
                               skipped=res.skipped, removed=res.removed)
    open(a.out, "w", encoding="utf-8").write(md)
    if a.json:
        json.dump([f.model_dump() for f in final], open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if a.meta:
        os.makedirs(os.path.dirname(os.path.abspath(a.meta)), exist_ok=True)
        json.dump({**res.meta(), "verify_mode": mode, "roles": roles}, open(a.meta, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n{len(final)} findings -> {a.out}")
    for f in final:
        print(f"  [{f.severity:8}] {f.file}:{f.line}  {f.title}  ({f.role}, {f.confidence:.0%}, ghim={f.anchor or '-'})")
    for s in res.skipped:
        print(f"  [bỏ qua] {s.path}: {s.reason} {s.detail}")
    if res.failed_roles:
        # báo kết quả KHÔNG đầy đủ để caller (vd eval) biết mà chạy lại
        print(f"\nCẢNH BÁO: {len(res.failed_roles)} vai lỗi: {', '.join(res.failed_roles)}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
