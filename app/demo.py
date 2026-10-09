"""Nút "Review thử" của website demo: POST /demo/review.

Khách (đã đăng nhập GitHub qua Firebase Auth) chọn một repo MẪU và một nhánh; server chạy đúng đường
`python -m app.cli` như eval, đẩy tiến độ theo thời gian thực vào Firestore (collection
`hermesqa_demo_jobs`) để trang web tự cập nhật, và kết quả đầy đủ nằm trong run log bình thường
(collection `hermesqa_runs`, cùng id với job vì cli tôn trọng HERMESQA_RUN_ID).

Ba hàng rào vì hạn mức LLM miễn phí là nút cổ chai:
  1. chỉ repo trong DEMO_REPOS (tên=đường dẫn cục bộ), nhánh phải là tên hợp lệ — không bao giờ clone URL lạ;
  2. mỗi người (uid Firebase, hoặc IP khi tắt xác thực) tối đa DEMO_MAX_PER_HOUR lần/giờ;
  3. một lúc chỉ chạy một review (409 nếu đang bận).

Module này KHÔNG sửa app/main.py: gắn vào app bằng `install(app)` (thêm CORS cho origin của Firebase
Hosting + router), hoặc chạy riêng khi phát triển: `python -m app.demo` (uvicorn, cổng 8010).
Cấu hình đọc từ .env với tiền tố DEMO_ (xem DemoSettings), độc lập với app.config để không đụng Settings chung.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("hermesqa.demo")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$")      # tên nhánh/commit an toàn để đưa vào argv của git


class DemoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    demo_repos: str = ""                  # "ten=duong/dan/repo,ten2=duong/dan/khac" — chỉ các repo này được review
    demo_max_per_hour: int = 3            # mỗi người dùng
    demo_require_auth: bool = True        # False = cho phép ẩn danh (chỉ khi phát triển cục bộ), giới hạn theo IP
    demo_skip_static: bool = True         # True = không cần Docker trên máy chủ demo
    demo_roles: str = ""                  # "" = để router tự chọn vai; "SE,QA" = ép danh sách vai
    demo_timeout: int = 900               # giây, quá thì giết tiến trình review
    demo_jobs_dir: str = "data/demo_jobs" # nơi ghi job khi không có Firestore
    demo_cors_origins: str = ""           # "https://<project>.web.app,https://<project>.firebaseapp.com"
    firebase_credentials: str = ""        # file service-account JSON (cùng biến với run log)
    firestore_jobs_collection: str = "hermesqa_demo_jobs"


demo_settings = DemoSettings()


# ---------------------------------------------------------------- repo mẫu

def parse_repos(spec: str) -> dict[str, str]:
    """'a=/path/a, b=D:\\x\\b' -> {'a': '/path/a', 'b': 'D:\\x\\b'}; bỏ qua mục không có dấu '=' hoặc đường dẫn không tồn tại."""
    out: dict[str, str] = {}
    for part in spec.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        name, path = part.split("=", 1)
        name, path = name.strip(), os.path.expanduser(path.strip())
        if name and os.path.isdir(path):
            out[name] = path
    return out


def list_branches(repo_path: str) -> list[str]:
    try:
        out = subprocess.check_output(["git", "-C", repo_path, "branch", "--format=%(refname:short)"],
                                      text=True, encoding="utf-8", errors="replace", timeout=20)
    except (subprocess.SubprocessError, OSError):
        return []
    return [b.strip() for b in out.splitlines() if b.strip()]


# ---------------------------------------------------------------- lưu job (Firestore hoặc file)

def _firebase_app(credentials_path: str):
    import firebase_admin
    from firebase_admin import credentials
    if not firebase_admin._apps:
        cred = credentials.Certificate(credentials_path) if credentials_path else credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)
    return firebase_admin.get_app()


class JobStore:
    """Mỗi job một document/file; `put` gộp trường (merge) để cập nhật tiến độ nhiều lần."""

    def __init__(self, credentials_path: str, collection: str, local_dir: str):
        self.local_dir = local_dir
        self.col = None
        if credentials_path:
            try:
                from firebase_admin import firestore
                _firebase_app(credentials_path)
                self.col = firestore.client().collection(collection)
            except Exception as e:  # không có Firestore thì vẫn chạy được demo cục bộ
                log.warning("Firestore không dùng được cho job demo (%s) -> ghi file %s", e, local_dir)
        if self.col is None:
            os.makedirs(local_dir, exist_ok=True)

    @property
    def kind(self) -> str:
        return "firestore" if self.col is not None else "local"

    def put(self, job_id: str, fields: dict) -> None:
        if self.col is not None:
            self.col.document(job_id).set(fields, merge=True)
            return
        cur = self.get(job_id) or {}
        cur.update(fields)
        tmp = os.path.join(self.local_dir, f"{job_id}.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cur, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(self.local_dir, f"{job_id}.json"))

    def get(self, job_id: str) -> dict | None:
        if self.col is not None:
            d = self.col.document(job_id).get()
            return d.to_dict() if d.exists else None
        p = os.path.join(self.local_dir, f"{job_id}.json")
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)


_store: JobStore | None = None


def get_store() -> JobStore:
    global _store
    if _store is None:
        _store = JobStore(demo_settings.firebase_credentials, demo_settings.firestore_jobs_collection,
                          demo_settings.demo_jobs_dir)
    return _store


# ---------------------------------------------------------------- giới hạn tần suất + xác thực

class RateLimiter:
    def __init__(self, max_per_hour: int):
        self.max = max_per_hour
        self.hits: dict[str, deque] = {}
        self.lock = threading.Lock()

    def retry_after(self, key: str, now: float | None = None) -> int:
        """0 nếu còn lượt, nếu không là số giây phải đợi."""
        now = now or time.time()
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and q[0] <= now - 3600:
                q.popleft()
            if len(q) < self.max:
                return 0
            return max(1, int(q[0] + 3600 - now))

    def hit(self, key: str, now: float | None = None) -> None:
        with self.lock:
            self.hits.setdefault(key, deque()).append(now or time.time())


limiter = RateLimiter(demo_settings.demo_max_per_hour)
_running = threading.Semaphore(1)


def verify_user(authorization: str | None, client_ip: str) -> dict:
    """Trả {'uid', 'name'}: từ ID token Firebase (Bearer) hoặc ẩn danh theo IP khi DEMO_REQUIRE_AUTH=false."""
    if not demo_settings.demo_require_auth and not authorization:
        return {"uid": f"anon:{client_ip}", "name": "khách"}
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Cần đăng nhập (thiếu ID token Firebase)")
    token = authorization.split(" ", 1)[1].strip()
    try:
        from firebase_admin import auth
        _firebase_app(demo_settings.firebase_credentials)
        claims = auth.verify_id_token(token)
    except ImportError:
        raise HTTPException(503, "Máy chủ chưa cài firebase-admin")
    except ValueError as e:                       # thiếu credentials / project
        raise HTTPException(503, f"Máy chủ chưa cấu hình Firebase: {e}")
    except Exception as e:                        # token hỏng, hết hạn, sai project
        raise HTTPException(401, f"ID token không hợp lệ: {type(e).__name__}")
    return {"uid": claims["uid"], "name": claims.get("name") or claims.get("email") or claims["uid"]}


# ---------------------------------------------------------------- chạy review và đẩy tiến độ

STEP_PATTERNS = [
    # (regex trên MỘT dòng stdout/stderr của app.cli, tên bước, chi tiết). Thứ tự = thứ tự ưu tiên.
    (re.compile(r"hermesqa\.sandbox"), "static", "quét công cụ tĩnh trong sandbox"),
    (re.compile(r"\[agent\] (\w+) LỖI"), "failed:{0}", "vai {0} lỗi"),
    (re.compile(r"\[agent\] (\w+) \.\.\."), "llm:{0}", "đang hỏi vai {0}"),
    (re.compile(r"(\d+) findings? ->"), "done", "{0} nhận xét"),
]
TOKEN_RE = re.compile(r"tokens in=(\d+) out=(\d+)")


def classify_line(line: str) -> tuple[str | None, str]:
    """Một dòng log của cli -> (tên bước hoặc None, chi tiết)."""
    for rx, step, detail in STEP_PATTERNS:
        m = rx.search(line)
        if m:
            return step.format(*m.groups()), detail.format(*m.groups())
    return None, ""


def _cli_cmd(repo_path: str, base: str, head: str, out_dir: str) -> list[str]:
    cmd = [sys.executable, "-m", "app.cli", "--repo", repo_path, "--base", base, "--head", head,
           "--out", os.path.join(out_dir, "report.md"), "--json", os.path.join(out_dir, "findings.json")]
    if demo_settings.demo_skip_static:
        cmd.append("--skip-static")
    if demo_settings.demo_roles:
        cmd += ["--roles", demo_settings.demo_roles]
    return cmd


class _Progress:
    """Gom các bước và dồn lại trước khi ghi (Firestore tính tiền theo lần ghi, và log token rất nhiều dòng)."""
    FLUSH_EVERY = 1.0

    def __init__(self, store: JobStore, job_id: str, t0: float):
        self.store, self.job_id, self.t0 = store, job_id, t0
        self.steps: list[dict] = []
        self.tail: deque[str] = deque(maxlen=30)
        self.tokens_in = self.tokens_out = 0
        self.current = "start"
        self._last_flush = 0.0
        self._dirty = False

    def line(self, raw: str) -> None:
        line = raw.rstrip()
        if not line:
            return
        self.tail.append(line[:300])
        m = TOKEN_RE.search(line)
        if m:
            self.tokens_in += int(m.group(1)); self.tokens_out += int(m.group(2))
        step, detail = classify_line(line)
        if step and step != self.current:
            self.current = step
            self.steps.append({"t": round(time.time() - self.t0, 1), "step": step, "detail": detail})
            self.flush(force=True)
            return
        self._dirty = True
        self.flush()

    def fields(self) -> dict:
        return {"current": self.current, "steps": self.steps[-60:], "log_tail": list(self.tail),
                "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "seconds": round(time.time() - self.t0, 1)}

    def flush(self, force: bool = False) -> None:
        now = time.time()
        if not force and (not self._dirty or now - self._last_flush < self.FLUSH_EVERY):
            return
        self._last_flush, self._dirty = now, False
        try:
            self.store.put(self.job_id, self.fields())
        except Exception as e:  # mất kết nối Firestore không được giết review
            log.warning("không cập nhật được job %s: %s", self.job_id, e)


def run_job(job_id: str, repo_name: str, repo_path: str, base: str, head: str, user: dict) -> None:
    """Chạy trong BackgroundTasks: subprocess app.cli, đọc từng dòng log -> tiến độ; cuối cùng ghi kết quả."""
    store = get_store()
    t0 = time.time()
    out_dir = tempfile.mkdtemp(prefix="hqa-demo-")
    prog = _Progress(store, job_id, t0)
    status, error, code = "failed", "", -1
    try:
        store.put(job_id, {"status": "running", "started_at": _now()})
        env = dict(os.environ, HERMESQA_RUN_ID=job_id, HERMESQA_SOURCE="demo", PYTHONIOENCODING="utf-8",
                   PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(_cli_cmd(repo_path, base, head, out_dir), cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        killer = threading.Timer(demo_settings.demo_timeout, proc.kill)
        killer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                prog.line(line)
            code = proc.wait()
        finally:
            timed_out = not killer.is_alive()
            killer.cancel()
        if timed_out:
            error = f"quá {demo_settings.demo_timeout}s, đã dừng"
        elif code in (0, 2):             # 2 = có vai lỗi nhưng vẫn có kết quả
            status = "done"
            if code == 2:
                error = "một số vai LLM lỗi, kết quả không đầy đủ"
        else:
            error = "\n".join(list(prog.tail)[-8:])
    except Exception as e:
        log.exception("job demo %s lỗi", job_id)
        error = f"{type(e).__name__}: {e}"
    finally:
        _running.release()
    result = {"status": status, "returncode": code, "error": error, "finished_at": _now(), **prog.fields()}
    try:
        fj = os.path.join(out_dir, "findings.json")
        if os.path.exists(fj):
            with open(fj, encoding="utf-8") as fh:
                findings = json.load(fh)
            result["findings_count"] = len(findings)
            result["findings"] = findings[:100]
        fm = os.path.join(out_dir, "report.md")
        if os.path.exists(fm):
            with open(fm, encoding="utf-8") as fh:
                result["report_md"] = fh.read()[:60000]
    except (OSError, json.JSONDecodeError) as e:
        result["error"] = (result["error"] + f"; không đọc được kết quả: {e}").strip("; ")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    try:
        store.put(job_id, result)
    except Exception as e:
        log.warning("không ghi được kết quả job %s: %s", job_id, e)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- HTTP

router = APIRouter(prefix="/demo", tags=["demo"])


class ReviewRequest(BaseModel):
    repo: str = Field(..., description="tên repo mẫu trong DEMO_REPOS")
    head: str = Field(..., description="nhánh/commit cần review")
    base: str = "main"


@router.get("/repos")
def demo_repos() -> dict:
    repos = parse_repos(demo_settings.demo_repos)
    return {"repos": [{"name": n, "branches": list_branches(p)} for n, p in repos.items()],
            "max_per_hour": demo_settings.demo_max_per_hour, "require_auth": demo_settings.demo_require_auth,
            "skip_static": demo_settings.demo_skip_static, "store": get_store().kind}


@router.get("/jobs/{job_id}")
def demo_job(job_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(404, "không có job này")
    job = get_store().get(job_id)
    if job is None:
        raise HTTPException(404, "không có job này")
    return job


@router.post("/review", status_code=202)
def demo_review(req: ReviewRequest, request: Request, background: BackgroundTasks,
                authorization: str | None = Header(None)) -> dict:
    user = verify_user(authorization, request.client.host if request.client else "?")
    repos = parse_repos(demo_settings.demo_repos)
    if req.repo not in repos:
        raise HTTPException(404, f"repo '{req.repo}' không nằm trong danh sách demo")
    for ref in (req.base, req.head):
        if not REF_RE.match(ref) or ".." in ref:
            raise HTTPException(422, f"tên nhánh không hợp lệ: {ref!r}")
    wait = limiter.retry_after(user["uid"])
    if wait:
        raise HTTPException(429, f"Mỗi người tối đa {demo_settings.demo_max_per_hour} lần/giờ, thử lại sau {wait // 60 + 1} phút",
                            headers={"Retry-After": str(wait)})
    if not _running.acquire(blocking=False):
        raise HTTPException(409, "Đang có một review chạy, thử lại sau ít phút")
    limiter.hit(user["uid"])
    job_id = uuid.uuid4().hex[:12]
    job = {"job_id": job_id, "status": "queued", "queued_at": _now(), "repo": req.repo,
           "base": req.base, "head": req.head, "user": user["name"], "uid": user["uid"],
           "skip_static": demo_settings.demo_skip_static, "steps": [], "current": "queued"}
    try:
        get_store().put(job_id, job)
    except Exception as e:
        _running.release()
        raise HTTPException(503, f"không ghi được job: {e}")
    background.add_task(run_job, job_id, req.repo, repos[req.repo], req.base, req.head, user)
    return {"job_id": job_id, "status": "queued", "store": get_store().kind}


def install(app: FastAPI) -> None:
    """Gắn router + CORS vào app FastAPI có sẵn (một dòng trong app/main.py)."""
    origins = [o.strip() for o in demo_settings.demo_cors_origins.split(",") if o.strip()]
    if origins:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "OPTIONS"],
                           allow_headers=["Authorization", "Content-Type"])
    app.include_router(router)


def create_app() -> FastAPI:
    app = FastAPI(title="HermesQA demo API", version="0.1.0")
    install(app)

    @app.get("/health")
    def health():
        return {"status": "ok", "store": get_store().kind}
    return app


if __name__ == "__main__":   # pragma: no cover — chạy riêng khi phát triển: python -m app.demo
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("DEMO_PORT", "8010")))
