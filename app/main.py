"""FastAPI entrypoint: nhận webhook GitHub, xác thực HMAC, đẩy job vào queue.
Trả 202 ngay lập tức (GitHub timeout webhook sau 10s)."""
import hashlib
import hmac
import logging

from fastapi import FastAPI, Header, HTTPException, Request
from redis import Redis
from rq import Queue

from app.config import settings
from app.demo import install as install_demo
from app.web.router import router as dashboard_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hermesqa.api")

app = FastAPI(title="HermesQA - AI Code Reviewer", version="0.1.0")
app.include_router(dashboard_router)       # trang web demo: /dashboard
install_demo(app)                          # nút "Review thử" của site Firebase: /demo/* + CORS (docs/FIREBASE_DEMO.md)
queue = Queue("reviews", connection=Redis.from_url(settings.redis_url))

REVIEW_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


def verify_signature(body: bytes, signature: str | None) -> None:
    if not signature or not signature.startswith("sha256="):
        raise HTTPException(401, "Missing signature")
    expected = hmac.new(settings.github_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.split("=", 1)[1]):
        raise HTTPException(401, "Bad signature")


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
