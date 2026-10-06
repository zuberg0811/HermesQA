"""RQ worker: pipeline review đầy đủ cho 1 PR (phần LLM dùng chung app.review.pipeline với CLI)."""
import logging
import os
import shutil
import subprocess
import yaml

from app.config import settings
from app.github_app import GitHubClient
from app.memory import init_db, save_run, get_repo_memory, get_posted, save_posted
from app.review import postprocess, report, static_tools
from app.review.agent import get_backend
from app.review.diff_utils import parse_diff
from app.review.pipeline import attach_code_to_static, run_llm_review, scope_static_to_diff
from app.review.router import select_roles
from app.review.schemas import ReviewConfig
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
        with open(p, encoding="utf-8") as f:
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

        # 3. Static analysis trong sandbox (chỉ giữ finding ở file thay đổi). Static quét CẢ file
        #    có thể chứa bí mật — sandbox không có mạng; chỉ LLM mới không được thấy chúng.
        run_static_analysis(repo_dir, out_dir, targets=sorted(changed_paths))
        all_static = collect_static(out_dir)
        static = scope_static_to_diff([f for f in all_static if f.file in changed_paths], files)
        tools = static_tools.tool_status(out_dir, all_static)
        for name, st in tools.items():
            if st["state"] == "failed":
                log.error("cong cu static THAT BAI: %s (ket qua thieu phan cua no)", name)
        attach_code_to_static(static, files)

        # 4. LLM theo từng vai: chọn file, che bí mật, chia lô, ghim, kiểm chứng (app.review.pipeline)
        roles = select_roles(files, cfg.roles)
        pr_meta = {"pr_number": pr, "title": job["title"], "body": job["body"], "owner": owner, "repo": repo, "head_sha": sha}
        res = run_llm_review(get_backend(), roles, files, repo_dir, pr_meta, static, cfg.conventions, get_repo_memory(full))
        summaries = {r: (s or "Không có nhận xét.") for r, s in res.summaries.items()}

        # 5. Post-process
        all_f = postprocess.dedupe(static + res.findings)
        inline, overflow = postprocess.apply_policy(all_f, files, settings.min_confidence, settings.min_severity_to_comment, max_comments)
        conclusion = postprocess.conclusion(inline + overflow, block)
        # Chỉ đăng comment CHƯA từng đăng lên PR này. Kết luận (conclusion) vẫn tính trên toàn bộ:
        # lỗi cũ chưa sửa vẫn phải chặn merge, chỉ là không nhắc lại lần nữa.
        to_post, repeated = postprocess.split_new(inline, get_posted(full, pr))
        for f in overflow:
            f.fingerprint = postprocess.fingerprint(f)

        # 6. Report
        os.makedirs(settings.report_dir, exist_ok=True)
        md = report.build_markdown(pr_meta, roles, summaries, inline + overflow, settings.llm_model, tools,
                                   skipped=res.skipped, removed=res.removed)
        report_path = os.path.join(settings.report_dir, f"{owner}_{repo}_pr{pr}_{sha[:8]}.md")
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(md)

        # 7. Đăng lên GitHub
        body = report.build_pr_summary(summaries, to_post, overflow, report_url=None,
                                       skipped=res.skipped, already_posted=len(repeated))
        event = "REQUEST_CHANGES" if conclusion == "failure" else "COMMENT"
        gh.post_review(owner, repo, pr, sha, body, [postprocess.to_github_comment(f) for f in to_post], event=event)
        gh.complete_check_run(owner, repo, check_id, conclusion, f"HermesQA: {len(inline)+len(overflow)} phát hiện", body, md)
        save_posted(full, pr, sha, [f.fingerprint for f in to_post])

        save_run(full, pr, sha, roles, inline + overflow, conclusion)
        log.info("done PR #%s conclusion=%s posted=%d repeated=%d overflow=%d skipped=%d report=%s",
                 pr, conclusion, len(to_post), len(repeated), len(overflow), len(res.skipped), report_path)

    except Exception as e:
        log.exception("review failed")
        gh.complete_check_run(owner, repo, check_id, "neutral", "HermesQA gặp lỗi", f"```\n{e}\n```")
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
