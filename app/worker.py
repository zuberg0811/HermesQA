"""RQ worker: pipeline review đầy đủ cho 1 PR."""
import logging
import os
import shutil
import subprocess
import yaml

from app.config import settings
from app.github_app import GitHubClient
from app.memory import init_db, save_run, get_repo_memory
from app.review import postprocess, report, static_tools
from app.review.agent import get_backend, build_user_prompt, self_verify
from app.review.diff_utils import parse_diff, file_context
from app.review.router import select_roles
from app.review.schemas import ReviewConfig, Finding
from app.review.static_tools import collect as collect_static
from app.sandbox import run_static_analysis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hermesqa.worker")


def _clone(clone_url: str, token: str, head_sha: str, dest: str):
    auth_url = clone_url.replace("https://", f"https://x-access-token:{token}@")
    subprocess.run(["git", "init", "-q", dest], check=True)
    subprocess.run(["git", "-C", dest, "remote", "add", "origin", auth_url], check=True)
    subprocess.run(["git", "-C", dest, "fetch", "-q", "--depth", "1", "origin", head_sha], check=True)
    subprocess.run(["git", "-C", dest, "checkout", "-q", "FETCH_HEAD"], check=True)
    subprocess.run(["git", "-C", dest, "remote", "remove", "origin"], check=True)  # không để token nằm lại trong .git


def _load_repo_config(repo_dir: str) -> ReviewConfig:
    p = os.path.join(repo_dir, ".hermesqa.yml")
    if os.path.exists(p):
        with open(p) as f:
            return ReviewConfig.model_validate(yaml.safe_load(f) or {})
    return ReviewConfig()


def review_pull_request(job: dict):
    init_db()
    gh = GitHubClient(job["installation_id"])
    owner, repo, pr, sha = job["owner"], job["repo"], job["pr_number"], job["head_sha"]
    full = f"{owner}/{repo}"
    check_id = gh.create_check_run(owner, repo, sha)

    work = os.path.join(settings.workdir, f"{owner}_{repo}_{pr}_{sha[:8]}")
    repo_dir, out_dir = os.path.join(work, "src"), os.path.join(work, "out")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)

    try:
        # 1. Clone + config
        _clone(job["clone_url"], gh.token(), sha, repo_dir)
        cfg = _load_repo_config(repo_dir)
        max_comments = cfg.max_comments or settings.max_comments_per_pr
        block = cfg.block_on_critical if cfg.block_on_critical is not None else settings.block_on_critical

        # 2. Diff
        files = [f for f in parse_diff(gh.get_pr_diff(owner, repo, pr), cfg.ignore) if not f.is_binary and not f.is_deleted]
        changed_paths = {f.path for f in files}
        if not files:
            gh.complete_check_run(owner, repo, check_id, "success", "Không có file cần review", "Chỉ có file binary/bị xoá/ignore.")
            return

        # 3. Static analysis trong sandbox (chỉ giữ finding ở file thay đổi)
        run_static_analysis(repo_dir, out_dir, targets=sorted(changed_paths))
        all_static = collect_static(out_dir)
        static = [f for f in all_static if f.file in changed_paths]
        tools = static_tools.tool_status(out_dir, all_static)
        for name, st in tools.items():
            if st["state"] == "failed":
                log.error("cong cu static THAT BAI: %s (ket qua thieu phan cua no)", name)
        static_summary = "\n".join(f"- {f.file}:{f.line} [{f.severity}] {f.title}: {f.explanation[:120]}" for f in static[:40])

        # 4. Agent theo từng vai
        roles = select_roles(files, cfg.roles)
        files_ctx = [{"path": f.path, "patch": f.patch_text[:8000], "context": file_context(repo_dir, f.path, f.added_lines)} for f in files[:25]]
        pr_meta = {"pr_number": pr, "title": job["title"], "body": job["body"], "owner": owner, "repo": repo, "head_sha": sha}
        backend = get_backend()
        llm_findings: list[Finding] = []
        summaries: dict[str, str] = {}
        for role in roles:
            prompt = build_user_prompt(pr_meta, files_ctx, static_summary, cfg.conventions, get_repo_memory(full))
            try:
                out = backend.run(role, prompt)
                out = self_verify(backend, role, out, files_ctx)
            except Exception as e:
                log.exception("role %s failed: %s", role, e)
                summaries[role] = f"(lỗi khi chạy: {e})"
                continue
            for f in out.findings:
                f.role, f.source = role, "llm"
            llm_findings += out.findings
            summaries[role] = out.summary or "Không có nhận xét."

        # 5. Post-process
        all_f = postprocess.dedupe(static + llm_findings)
        inline, overflow = postprocess.apply_policy(all_f, files, settings.min_confidence, settings.min_severity_to_comment, max_comments)
        conclusion = postprocess.conclusion(inline + overflow, block)

        # 6. Report
        os.makedirs(settings.report_dir, exist_ok=True)
        md = report.build_markdown(pr_meta, roles, summaries, inline + overflow, settings.llm_model, tools)
        report_path = os.path.join(settings.report_dir, f"{owner}_{repo}_pr{pr}_{sha[:8]}.md")
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(md)

        # 7. Đăng lên GitHub
        body = report.build_pr_summary(summaries, inline, overflow, report_url=None)
        event = "REQUEST_CHANGES" if conclusion == "failure" else "COMMENT"
        gh.post_review(owner, repo, pr, sha, body, [postprocess.to_github_comment(f) for f in inline], event=event)
        gh.complete_check_run(owner, repo, check_id, conclusion, f"HermesQA: {len(inline)+len(overflow)} phát hiện", body, md)

        save_run(full, pr, sha, roles, inline + overflow, conclusion)
        log.info("done PR #%s conclusion=%s inline=%d overflow=%d report=%s", pr, conclusion, len(inline), len(overflow), report_path)

    except Exception as e:
        log.exception("review failed")
        gh.complete_check_run(owner, repo, check_id, "neutral", "HermesQA gặp lỗi", f"```\n{e}\n```")
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
