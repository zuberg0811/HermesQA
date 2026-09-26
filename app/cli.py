"""CLI chạy review offline trên repo local — dùng để dev nhanh và chạy eval cho đồ án.
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

from app.config import settings
from app.review import postprocess, report, static_tools
from app.review.agent import get_backend, build_user_prompt, self_verify
from app.review.diff_utils import parse_diff, file_context
from app.review.router import select_roles
from app.review.schemas import ReviewConfig
from app.review.static_tools import collect as collect_static
from app.sandbox import run_static_analysis
import yaml


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", default="main")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--diff-file", help="dùng diff sẵn thay vì git diff")
    ap.add_argument("--out", default="report.md")
    ap.add_argument("--json", help="ghi findings ra JSON (cho eval)")
    ap.add_argument("--skip-static", action="store_true")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--roles", help="VD: SE,QA")
    a = ap.parse_args()

    if a.diff_file:
        diff = open(a.diff_file, encoding="utf-8").read()
    else:
        diff = subprocess.check_output(["git", "-C", a.repo, "diff", f"{a.base}...{a.head}"], text=True)

    cfg_path = os.path.join(a.repo, ".hermesqa.yml")
    cfg = ReviewConfig.model_validate(yaml.safe_load(open(cfg_path)) or {}) if os.path.exists(cfg_path) else ReviewConfig()
    files = [f for f in parse_diff(diff, cfg.ignore) if not f.is_binary and not f.is_deleted]
    if not files:
        print("Không có file để review"); return
    changed = {f.path for f in files}

    static = []
    tools = None
    if not a.skip_static:
        out_dir = tempfile.mkdtemp(prefix="hqa-out-")
        run_static_analysis(a.repo, out_dir, targets=sorted(changed))
        all_static = collect_static(out_dir)
        static = [f for f in all_static if f.file in changed]
        tools = static_tools.tool_status(out_dir, all_static)
        for name, st in tools.items():
            if st["state"] == "failed":
                log.error("cong cu static THAT BAI: %s (ket qua thieu phan cua no)", name)
    static_summary = "\n".join(f"- {f.file}:{f.line} [{f.severity}] {f.title}: {f.explanation[:120]}" for f in static[:40])

    roles = [r for r in a.roles.split(",") if r.strip()] if a.roles is not None else select_roles(files, cfg.roles)
    files_ctx = [{"path": f.path, "patch": f.patch_text[:8000], "context": file_context(a.repo, f.path, f.added_lines)} for f in files[:25]]
    pr_meta = {"pr_number": 0, "title": f"{a.base}..{a.head}", "body": "", "owner": "local", "repo": os.path.basename(os.path.abspath(a.repo)), "head_sha": a.head}

    backend = get_backend()
    llm, summaries = [], {}
    failed_roles = []
    for role in roles:
        print(f"[agent] {role} ...")
        try:
            out = backend.run(role, build_user_prompt(pr_meta, files_ctx, static_summary, cfg.conventions, ""))
            if not a.no_verify:
                out = self_verify(backend, role, out, files_ctx)
        except Exception as e:  # một vai lỗi (LLM quá tải, JSON hỏng) không được làm hỏng cả review
            print(f"[agent] {role} LỖI: {e}")
            summaries[role] = f"(lỗi khi chạy: {e})"
            failed_roles.append(role)
            continue
        for f in out.findings:
            f.role, f.source = role, "llm"
        llm += out.findings
        summaries[role] = out.summary

    all_f = postprocess.dedupe(static + llm)
    inline, overflow = postprocess.apply_policy(all_f, files, settings.min_confidence, settings.min_severity_to_comment, cfg.max_comments or settings.max_comments_per_pr)
    md = report.build_markdown(pr_meta, roles, summaries, inline + overflow, settings.llm_model, tools)
    open(a.out, "w", encoding="utf-8").write(md)
    if a.json:
        json.dump([f.model_dump() for f in inline + overflow], open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n{len(inline)+len(overflow)} findings -> {a.out}")
    for f in inline + overflow:
        print(f"  [{f.severity:8}] {f.file}:{f.line}  {f.title}  ({f.role}, {f.confidence:.0%})")
    if failed_roles:
        # báo kết quả KHÔNG đầy đủ để caller (vd eval) biết mà chạy lại
        print(f"\nCẢNH BÁO: {len(failed_roles)} vai lỗi: {', '.join(failed_roles)}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
