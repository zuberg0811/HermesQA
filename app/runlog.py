"""Nhật ký mỗi lần chạy review (run log) cho dashboard: ai chạy, trên gì, từng vai làm gì, tốn bao nhiêu.

Vì sao tách khỏi memory.py: bảng `reviews` trong Postgres phục vụ bot (lịch sử PR, dấu vân tay comment);
run log phục vụ TRANG WEB DEMO và cần (a) ghi được từ cả CLI lẫn worker, (b) chứa thống kê theo vai để
"chấm điểm từng agent", (c) nằm ở nơi web public đọc được — Firestore theo kế hoạch demo, hoặc file JSON
cục bộ khi phát triển/eval. Một document = một lần chạy, không có schema cứng ngoài các trường dưới đây.

Cấu hình (app/config.py): RUNLOG_BACKEND=auto|local|firestore|off, RUNLOG_DIR, FIREBASE_CREDENTIALS
(file service-account JSON), FIRESTORE_COLLECTION. `auto` = firestore nếu có credentials, không thì local.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Protocol

from app.config import settings

log = logging.getLogger("hermesqa.runlog")

ROLES = ("SE", "QA", "DevOps")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    """Id lần chạy: lấy từ HERMESQA_RUN_ID nếu có (job demo trên web và run log dùng chung id)."""
    return os.environ.get("HERMESQA_RUN_ID") or uuid.uuid4().hex[:12]


def build_run_record(*, source: str, pr_meta: dict, roles: list[str], res, static, final, tools,
                     seconds: float, model: str, verify_mode: str, skip_static: bool,
                     conclusion: str | None = None, extra: dict | None = None,
                     run_id: str | None = None, started_at: str | None = None) -> dict:
    """Gom mọi thứ một lần chạy sinh ra thành một document phẳng, JSON được.

    `res` là app.review.pipeline.LLMReview; `static` là finding static đã lọc; `final` là finding sau
    dedupe + policy (thứ người dùng nhìn thấy); `tools` là trạng thái công cụ static (có thể None).
    """
    role_stats = {r: dict(c) for r, c in getattr(res, "role_stats", {}).items()}
    for r in roles:
        role_stats.setdefault(r, {})
    for r, rs in role_stats.items():
        rs["final"] = sum(1 for f in final if f.source == "llm" and f.role == r)
    static_by_tool: dict[str, int] = {}
    for s in static:
        static_by_tool[s.source] = static_by_tool.get(s.source, 0) + 1
    rec = {
        "run_id": run_id or new_run_id(),
        "started_at": started_at or _now(),
        "status": "done",
        "source": source,                                   # cli | worker | eval
        "repo": f"{pr_meta.get('owner', '')}/{pr_meta.get('repo', '')}".strip("/"),
        "pr_number": pr_meta.get("pr_number", 0),
        "title": pr_meta.get("title", ""),
        "head_sha": pr_meta.get("head_sha", ""),
        "model": model,
        "config": {"verify_mode": verify_mode, "skip_static": skip_static, "roles": roles},
        "seconds": round(seconds, 1),
        "stats": dict(getattr(res, "stats", {})),
        "role_stats": role_stats,
        "static_by_tool": static_by_tool,
        "tools": tools or {},
        "failed_roles": list(getattr(res, "failed_roles", [])),
        "summaries": dict(getattr(res, "summaries", {})),
        "findings": [f.model_dump() for f in final],
        "removed": list(getattr(res, "removed", [])),
        "skipped": [s.__dict__ for s in getattr(res, "skipped", [])],
        "conclusion": conclusion,
        "counts": {
            "static": len(static),
            "llm_raw": sum(rs.get("raw", 0) for rs in role_stats.values()),
            "llm_kept": sum(rs.get("kept", 0) for rs in role_stats.values()),
            "final": len(final),
            "by_severity": _count(final, "severity"),
            "by_category": _count(final, "category"),
        },
    }
    if extra:
        rec.update(extra)
    return rec


def _count(findings, attr: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in findings:
        k = getattr(f, attr, None) or "?"
        out[k] = out.get(k, 0) + 1
    return out


class RunStore(Protocol):
    def save(self, rec: dict) -> str: ...
    def update(self, run_id: str, patch: dict) -> None: ...
    def list_runs(self, limit: int = 50) -> list[dict]: ...
    def get(self, run_id: str) -> dict | None: ...


class NullStore:
    def save(self, rec: dict) -> str:
        return rec["run_id"]

    def update(self, run_id: str, patch: dict) -> None:
        pass

    def list_runs(self, limit: int = 50) -> list[dict]:
        return []

    def get(self, run_id: str) -> dict | None:
        return None


class LocalJsonStore:
    """Mỗi lần chạy một file <dir>/<started_at>_<run_id>.json. Đủ cho phát triển và eval cục bộ."""
    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, run_id: str) -> str | None:
        for n in self._files():
            if n.endswith(f"_{run_id}.json"):
                return os.path.join(self.dir, n)
        return None

    def save(self, rec: dict) -> str:
        path = self._path(rec["run_id"]) or os.path.join(self.dir, f"{rec['started_at'].replace(':', '-')}_{rec['run_id']}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=1)
        return rec["run_id"]

    def update(self, run_id: str, patch: dict) -> None:
        path = self._path(run_id)
        if path is None:
            return
        cur = json.load(open(path, encoding="utf-8"))
        cur.update(patch)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cur, fh, ensure_ascii=False, indent=1)

    def _files(self) -> list[str]:
        return sorted((n for n in os.listdir(self.dir) if n.endswith(".json")), reverse=True)

    def list_runs(self, limit: int = 50) -> list[dict]:
        out = []
        for n in self._files()[:limit]:
            try:
                out.append(_summary(json.load(open(os.path.join(self.dir, n), encoding="utf-8"))))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def get(self, run_id: str) -> dict | None:
        path = self._path(run_id)
        return json.load(open(path, encoding="utf-8")) if path else None


class FirestoreStore:
    """Mỗi lần chạy một document trong collection; findings/removed/skipped nằm trong cùng document
    (giới hạn 1 MiB/document của Firestore — vượt quá thì cắt bớt findings, có đánh dấu)."""
    MAX_FINDINGS = 300

    def __init__(self, credentials_path: str, collection: str):
        import firebase_admin
        from firebase_admin import credentials, firestore
        if not firebase_admin._apps:
            cred = credentials.Certificate(credentials_path) if credentials_path else credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred)
        self.db = firestore.client()
        self.col = self.db.collection(collection)

    def save(self, rec: dict) -> str:
        doc = dict(rec)
        if len(doc.get("findings", [])) > self.MAX_FINDINGS:
            doc["findings"] = doc["findings"][:self.MAX_FINDINGS]
            doc["findings_truncated"] = True
        self.col.document(rec["run_id"]).set(doc, merge=True)
        return rec["run_id"]

    def update(self, run_id: str, patch: dict) -> None:
        self.col.document(run_id).set(patch, merge=True)

    def list_runs(self, limit: int = 50) -> list[dict]:
        from firebase_admin import firestore
        q = self.col.order_by("started_at", direction=firestore.Query.DESCENDING).limit(limit)
        return [_summary(d.to_dict()) for d in q.stream()]

    def get(self, run_id: str) -> dict | None:
        d = self.col.document(run_id).get()
        return d.to_dict() if d.exists else None


def _summary(rec: dict) -> dict:
    """Bản rút gọn cho danh sách (không kèm findings)."""
    keep = ("run_id", "started_at", "status", "current", "source", "repo", "pr_number", "title", "model", "config",
            "seconds", "counts", "role_stats", "failed_roles", "conclusion", "eval_case", "eval_config")
    return {k: rec.get(k) for k in keep if k in rec}


_store: RunStore | None = None


def get_store() -> RunStore:
    global _store
    if _store is not None:
        return _store
    backend = settings.runlog_backend
    if backend == "auto":
        backend = "firestore" if settings.firebase_credentials else "local"
    if backend == "off":
        _store = NullStore()
    elif backend == "firestore":
        try:
            _store = FirestoreStore(settings.firebase_credentials, settings.firestore_collection)
        except Exception as e:   # thiếu thư viện / credentials sai: KHÔNG được làm hỏng review
            log.warning("Firestore không dùng được (%s) -> ghi run log cục bộ vào %s", e, settings.runlog_dir)
            _store = LocalJsonStore(settings.runlog_dir)
    else:
        _store = LocalJsonStore(settings.runlog_dir)
    return _store


def record_run(**kwargs) -> str | None:
    """Ghi một lần chạy; lỗi ghi log chỉ cảnh báo, không bao giờ làm review thất bại."""
    try:
        rec = build_run_record(**kwargs)
        rid = get_store().save(rec)
        log.info("run log %s -> %s", rid, type(get_store()).__name__)
        return rid
    except Exception as e:
        log.warning("không ghi được run log: %s", e)
        return None


class RunContext:
    """Nhật ký có tiến độ: tạo document lúc BẮT ĐẦU (status=running), ghi từng bước, kết thúc bằng finish().

    Trang web realtime đọc `status`, `current`, `steps` trong lúc chạy; sau finish() document có đủ các
    trường của build_run_record. Mọi lỗi ghi đều nuốt và cảnh báo — review không được thất bại vì log.
    """
    def __init__(self, source: str, pr_meta: dict, model: str):
        self.run_id = new_run_id()
        self.started_at = _now()
        self.steps: list[dict] = []
        self._t0 = time.time()
        self._head = {"run_id": self.run_id, "started_at": self.started_at, "status": "running", "source": source,
                      "repo": f"{pr_meta.get('owner', '')}/{pr_meta.get('repo', '')}".strip("/"),
                      "pr_number": pr_meta.get("pr_number", 0), "title": pr_meta.get("title", ""),
                      "head_sha": pr_meta.get("head_sha", ""), "model": model, "steps": [], "current": "start",
                      "counts": {"static": 0, "llm_raw": 0, "llm_kept": 0, "final": 0}, "config": {"roles": []},
                      "role_stats": {}, "failed_roles": [], "seconds": 0}
        self._safe(lambda: get_store().save(dict(self._head)))

    def _safe(self, fn):
        try:
            fn()
        except Exception as e:
            log.warning("run log %s: %s", self.run_id, e)

    def step(self, name: str, detail: str = "") -> None:
        self.steps.append({"t": _now(), "step": name, "detail": detail, "elapsed": round(time.time() - self._t0, 1)})
        self._safe(lambda: get_store().update(self.run_id, {"steps": self.steps, "current": name}))

    def finish(self, **kwargs) -> str:
        """kwargs như build_run_record (trừ run_id/started_at/seconds — tự điền)."""
        rec = build_run_record(run_id=self.run_id, started_at=self.started_at, seconds=time.time() - self._t0, **kwargs)
        rec["steps"] = self.steps + [{"t": _now(), "step": "done", "detail": "", "elapsed": rec["seconds"]}]
        rec["current"] = "done"
        self._safe(lambda: get_store().save(rec))
        return self.run_id

    def fail(self, error: str) -> None:
        self.steps.append({"t": _now(), "step": "failed", "detail": error[:300], "elapsed": round(time.time() - self._t0, 1)})
        self._safe(lambda: get_store().update(self.run_id, {"status": "failed", "current": "failed", "steps": self.steps,
                                                           "error": error[:1000], "seconds": round(time.time() - self._t0, 1)}))
