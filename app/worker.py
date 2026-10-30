"""RQ worker: pipeline review đầy đủ cho 1 PR (phần LLM dùng chung app.review.pipeline với CLI)."""
import logging
import os
import shutil
import subprocess
import yaml

from app import feedback
from app.config import settings
from app.github_app import GitHubClient
from app.memory import init_db, save_run, memory_for_batch, get_posted, save_posted, feedback_profile
from app.review import calibrate, postprocess, reflect, report, stale, static_tools
from app.review.agent import get_backend
from app.review.diff_utils import parse_diff
from app.review.pipeline import attach_code_to_static, run_llm_review, scope_static_to_diff
from app.review.router import select_roles
from app.review.schemas import ReviewConfig
from app.review.static_tools import collect as collect_static
from app.runlog import RunContext
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

    pr_meta = {"pr_number": pr, "title": job["title"], "body": job["body"], "owner": owner, "repo": repo, "head_sha": sha}
    ctx = RunContext("worker", pr_meta, settings.llm_model)
    try:
        # 1. Clone + config
        ctx.step("clone", sha[:8])
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
        ctx.step("static", "quét trong sandbox")
        run_static_analysis(repo_dir, out_dir, targets=sorted(changed_paths))
        all_static = collect_static(out_dir)
        static = scope_static_to_diff([f for f in all_static if f.file in changed_paths], files)
        tools = static_tools.tool_status(out_dir, all_static)
        for name, st in tools.items():
            if st["state"] == "failed":
                log.error("cong cu static THAT BAI: %s (ket qua thieu phan cua no)", name)
        # 3a. Khoá/chứng chỉ hết hạn + code lỗi thời (app.review.stale): thuần Python, lỗi ở đây không làm hỏng review
        if settings.stale_enabled:
            try:
                stale_f = stale.scan(files, repo_dir)
                static += stale_f
                tools["stale"] = {"state": "ok", "findings": len(stale_f)}
            except Exception as e:
                log.warning("stale scan failed: %s", e)
                tools["stale"] = {"state": "failed", "findings": 0}
        attach_code_to_static(static, files)

        # 3b. Học từ 👍/👎 người đã bấm lên comment của lần chạy trước (GitHub không có webhook cho
        #     reaction). Làm TRƯỚC khi gọi LLM để ghi chú mới áp dụng ngay cho lần review này.
        try:
            learned = feedback.sync_reactions(gh, owner, repo, pr)
            if learned:
                ctx.step("feedback", f"học {learned} phản hồi 👍/👎 từ lần trước")
        except Exception as e:  # học hỏi không được làm hỏng review
            log.warning("sync_reactions failed: %s", e)

        # 4. LLM theo từng vai: chọn file, che bí mật, chia lô, ghim, kiểm chứng (app.review.pipeline)
        roles = select_roles(files, cfg.roles)
        ctx.step("static:done", f"{len(static)} finding")
        backend = get_backend()
        # memory_for_batch: mỗi lô file nhận ghi chú liên quan tới chính nó (lớp 3 tự học), không phải 20 ghi chú mới nhất
        res = run_llm_review(backend, roles, files, repo_dir, pr_meta, static, cfg.conventions, memory_for_batch(full), on_step=ctx.step)
        summaries = {r: (s or "Không có nhận xét.") for r, s in res.summaries.items()}

        # 5. Post-process
        ctx.step("postprocess", "gộp trùng, lọc theo ngưỡng")
        all_f = postprocess.dedupe(static + res.findings)
        # 5b. Lớp 5 tự học: loại nhận xét repo đã 👎 nhiều lần bị hạ confidence (app.review.calibrate),
        #     nên rơi khỏi ngưỡng min_confidence ở bước ngay dưới mà không cần model "nhớ".
        adjusted: list[dict] = []
        try:
            adjusted = calibrate.calibrate(all_f, calibrate.Profile(feedback_profile(full)),
                                           settings.feedback_min_rejects, settings.feedback_reject_ratio,
                                           settings.feedback_confidence_factor)
            if adjusted:
                ctx.step("calibrate", f"hạ confidence {len(adjusted)} nhận xét theo 👎 trước đây")
        except Exception as e:  # học hỏi không được làm hỏng review
            log.warning("calibrate failed: %s", e)
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
        ctx.step("post", f"đăng {len(to_post)} comment lên GitHub")
        review = gh.post_review(owner, repo, pr, sha, body, [postprocess.to_github_comment(f) for f in to_post], event=event)
        gh.complete_check_run(owner, repo, check_id, conclusion, f"HermesQA: {len(inline)+len(overflow)} phát hiện", body, md)
        save_posted(full, pr, sha, to_post)   # lưu cả tóm tắt finding để phản hồi sau này biết nó nói về gì
        try:   # gắn id comment GitHub vào từng finding (reply của người sẽ trỏ về id này)
            feedback.link_comment_ids(gh, owner, repo, pr, (review or {}).get("id"))
        except Exception as e:
            log.warning("link_comment_ids failed: %s", e)

        save_run(full, pr, sha, roles, inline + overflow, conclusion)

        # 8. Lớp 2 tự học: agent nhìn lại lần chạy (giữ gì, fact-check loại gì) và tự ghi 0-3 ghi chú
        #    về repo cho lần sau (app.review.reflect). Sau khi đã đăng, nên chậm cũng không ảnh hưởng người dùng.
        learned: list[dict] = []
        if settings.agent_memory_enabled:
            ctx.step("reflect", "agent tự rút ghi chú về repo")
            learned = reflect.learn(backend, full, pr_meta, inline + overflow, res.removed, cfg.conventions,
                                    max_notes=settings.agent_memory_max_notes)

        ctx.finish(source="worker", pr_meta=pr_meta, roles=roles, res=res, static=static, final=inline + overflow,
                   tools=tools, model=settings.llm_model, verify_mode=settings.verify_mode, skip_static=False,
                   conclusion=conclusion, extra={"posted": len(to_post), "repeated": len(repeated),
                                                 "calibrated": len(adjusted), "learned_notes": len(learned)})
        log.info("done PR #%s conclusion=%s posted=%d repeated=%d overflow=%d skipped=%d report=%s",
                 pr, conclusion, len(to_post), len(repeated), len(overflow), len(res.skipped), report_path)

    except Exception as e:
        ctx.fail(str(e))
        log.exception("review failed")
        gh.complete_check_run(owner, repo, check_id, "neutral", "HermesQA gặp lỗi", f"```\n{e}\n```")
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def ingest_feedback(job: dict):
    """Job nhẹ: người trả lời vào comment của bot -> phân loại -> ghi repo memory (app.feedback)."""
    init_db()
    gh = GitHubClient(job["installation_id"])
    kind = feedback.ingest_reply(gh, job["owner"], job["repo"], job["pr_number"], job["comment"])
    log.info("feedback PR #%s %s/%s -> %s", job["pr_number"], job["owner"], job["repo"], kind or "ignored")
    return kind
