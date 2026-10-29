"""FastAPI entrypoint: nhận webhook GitHub, xác thực HMAC, đẩy job vào queue.
Trả 202 ngay lập tức (GitHub timeout webhook sau 10s)."""
import hashlib
import hmac
import logging
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from redis import Redis
from rq import Queue

from app.config import settings
from app.demo import install as install_demo
from app.web.router import router as dashboard_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hermesqa.api")

STATIC_DIR = Path(__file__).parent / "web" / "static"

app = FastAPI(title="HermesQA - AI Code Reviewer", version="0.1.0")
app.include_router(dashboard_router)       # trang kỹ thuật: /dashboard
install_demo(app)                          # nút "Kiểm tra code": /demo/* (+ CORS nếu có site ngoài, docs/FIREBASE_DEMO.md)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
queue = Queue("reviews", connection=Redis.from_url(settings.redis_url))


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    """Trang cho người dùng phổ thông: đưa code vào, xem kết quả tại chỗ (app/web/static/home.html)."""
    return FileResponse(STATIC_DIR / "home.html", media_type="text/html; charset=utf-8")

REVIEW_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


def verify_signature(body: bytes, signature: str | None) -> None:
    if not signature or not signature.startswith("sha256="):
        raise HTTPException(401, "Missing signature")
    expected = hmac.new(settings.github_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.split("=", 1)[1]):
        raise HTTPException(401, "Bad signature")


def _enqueue_feedback(payload: dict, delivery_id: str):
    """Người trả lời vào một comment inline của bot -> job học phản hồi (app.feedback). Nhẹ, không clone repo.
    Reaction 👍/👎 không có webhook; worker tự đọc lại ở lần push sau."""
    comment = payload.get("comment") or {}
    if payload.get("action") != "created" or not comment.get("in_reply_to_id"):
        return {"msg": "ignored", "event": "pull_request_review_comment", "action": payload.get("action")}
    if (comment.get("user") or {}).get("type") == "Bot":
        return {"msg": "ignored bot comment"}
    job_payload = {
        "delivery_id": delivery_id,
        "installation_id": payload["installation"]["id"],
        "owner": payload["repository"]["owner"]["login"],
        "repo": payload["repository"]["name"],
        "pr_number": payload["pull_request"]["number"],
        "comment": {"id": comment.get("id"), "in_reply_to_id": comment.get("in_reply_to_id"),
                    "body": comment.get("body") or "", "user": comment.get("user") or {}},
    }
    job = queue.enqueue("app.worker.ingest_feedback", job_payload, job_timeout="2m")
    log.info("queued feedback PR #%s %s/%s job=%s", job_payload["pr_number"], job_payload["owner"], job_payload["repo"], job.id)
    return {"msg": "queued feedback", "job_id": job.id}


@app.get("/health")
def health():
    return {"status": "ok", "queued": queue.count}


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(...),
    x_hub_signature_256: str | None = Header(None),
    x_github_delivery: str = Header("unknown"),
):
    body = await request.body()
    verify_signature(body, x_hub_signature_256)
    payload = await request.json()

    if x_github_event == "ping":
        return {"msg": "pong"}

    if x_github_event == "pull_request_review_comment":
        return _enqueue_feedback(payload, x_github_delivery)

    if x_github_event != "pull_request" or payload.get("action") not in REVIEW_ACTIONS:
        return {"msg": "ignored", "event": x_github_event, "action": payload.get("action")}

    pr = payload["pull_request"]
    if pr.get("draft"):
        return {"msg": "ignored draft"}

    job_payload = {
        "delivery_id": x_github_delivery,
        "installation_id": payload["installation"]["id"],
        "owner": payload["repository"]["owner"]["login"],
        "repo": payload["repository"]["name"],
        "clone_url": payload["repository"]["clone_url"],
        "pr_number": pr["number"],
        "head_sha": pr["head"]["sha"],
        "base_sha": pr["base"]["sha"],
        "title": pr["title"],
        "body": pr.get("body") or "",
    }
    job = queue.enqueue("app.worker.review_pull_request", job_payload, job_timeout="15m")
    log.info("queued PR #%s %s/%s job=%s", pr["number"], job_payload["owner"], job_payload["repo"], job.id)
    return {"msg": "queued", "job_id": job.id}
