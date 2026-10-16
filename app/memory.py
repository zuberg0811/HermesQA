"""Bộ nhớ theo repo: convention/feedback học được, và dấu vân tay comment đã đăng (để không đăng lại).
MVP dùng Postgres qua SQLAlchemy; Hermes Agent có memory riêng nhưng ta giữ 1 lớp độc lập để không lock-in.

Ba bảng:
- review_runs      lịch sử mỗi lần review một PR
- posted_comments  mỗi comment inline đã đăng: dấu vân tay + id comment trên GitHub + tóm tắt finding
                   (để khi người dùng phản hồi vào comment đó, ta biết nó nói về finding nào)
- repo_memory      ghi chú ngắn được nạp vào prompt ở lần review sau (app.feedback ghi, pipeline đọc)
- feedback         từng phản hồi thô (reply / reaction) đã ghi nhận, để không ghi nhận hai lần
"""
import logging
from datetime import datetime
from sqlalchemy import create_engine, inspect, text, Column, Integer, String, Text, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

log = logging.getLogger("hermesqa.memory")
Base = declarative_base()
engine = create_engine(settings.database_url, pool_pre_ping=True)
Session = sessionmaker(bind=engine)


class ReviewRun(Base):
    __tablename__ = "review_runs"
    id = Column(Integer, primary_key=True)
    repo = Column(String, index=True)
    pr_number = Column(Integer)
    head_sha = Column(String)
    roles = Column(JSON)
    findings = Column(JSON)
    conclusion = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class RepoMemory(Base):
    __tablename__ = "repo_memory"
    id = Column(Integer, primary_key=True)
    repo = Column(String, index=True)
    note = Column(Text)                 # ví dụ: "Team chấp nhận dùng print() trong scripts/"
    source = Column(String)             # feedback | agent | manual
    kind = Column(String, default="")   # reject (không phải lỗi) | accept (đúng là lỗi) | "" (ghi chú tự do)
    fingerprint = Column(String, default="")   # finding gốc, nếu ghi chú sinh ra từ một comment cụ thể
    file = Column(String, default="")
    category = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class PostedComment(Base):
    """Dấu vân tay của mỗi comment inline đã đăng lên một PR (xem postprocess.fingerprint)."""
    __tablename__ = "posted_comments"
    id = Column(Integer, primary_key=True)
    repo = Column(String, index=True)
    pr_number = Column(Integer, index=True)
    fingerprint = Column(String, index=True)
    head_sha = Column(String)
    comment_id = Column(Integer, index=True, nullable=True)   # id review comment trên GitHub, điền sau khi đăng
    file = Column(String, default="")
    category = Column(String, default="")
    title = Column(String, default="")
    source = Column(String, default="")
    severity = Column(String, default="")
    role = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class Feedback(Base):
    """Một phản hồi của người trên một comment của bot. (comment_id, channel, kind) là duy nhất:
    một reaction 👎 chỉ ghi nhận một lần dù lần chạy sau vẫn thấy nó."""
    __tablename__ = "feedback"
    __table_args__ = (UniqueConstraint("comment_id", "channel", "kind", name="uq_feedback_once"),)
    id = Column(Integer, primary_key=True)
    repo = Column(String, index=True)
    pr_number = Column(Integer)
    fingerprint = Column(String, index=True)
    comment_id = Column(Integer, index=True)
    kind = Column(String)               # reject | accept
    channel = Column(String)            # reply | reaction
    author = Column(String, default="")
    body = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


def _add_missing_columns():
    """create_all không thêm cột vào bảng đã có. Với DB cũ (trước khi có feedback) bổ sung cột thiếu."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in (PostedComment, RepoMemory):
            if not insp.has_table(table.__tablename__):
                continue
            have = {c["name"] for c in insp.get_columns(table.__tablename__)}
            for col in table.__table__.columns:
                if col.name in have:
                    continue
                ddl = f"ALTER TABLE {table.__tablename__} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}"
                log.info("migrate: %s", ddl)
                conn.execute(text(ddl))


def init_db():
    _add_missing_columns()
    Base.metadata.create_all(engine)


def save_run(repo: str, pr_number: int, head_sha: str, roles: list, findings: list, conclusion: str):
    with Session() as s:
        s.add(ReviewRun(repo=repo, pr_number=pr_number, head_sha=head_sha, roles=roles,
                        findings=[f.model_dump() for f in findings], conclusion=conclusion))
        s.commit()


# ---------- repo memory ----------

def list_memory(repo: str, limit: int = 20) -> list[dict]:
    """Ghi chú mới nhất trước."""
    with Session() as s:
        rows = s.query(RepoMemory).filter_by(repo=repo).order_by(RepoMemory.created_at.desc(), RepoMemory.id.desc()).limit(limit).all()
        return [{"note": r.note, "source": r.source or "", "kind": r.kind or "", "file": r.file or "",
                 "category": r.category or ""} for r in rows]


def get_repo_memory(repo: str, limit: int = 20) -> str:
    """Khối text nạp vào prompt. Ghi chú do agent tự rút ra được đánh dấu để model biết nó chưa
    được người xác nhận (ghi chú từ phản hồi người đã tự nói rõ "Người review nói...")."""
    lines = []
    for r in list_memory(repo, limit):
        prefix = "(tự rút ra) " if r["source"] == "agent" else ""
        lines.append(f"- {prefix}{r['note']}")
    return "\n".join(lines)


def _path_score(note_file: str, paths: list[str]) -> int:
    """3 = trùng file, 2 = cùng thư mục / tiền tố, 1 = ghi chú chung (không gắn file), 0 = file khác."""
    nf = (note_file or "").replace("\\", "/").strip().rstrip("/")
    if not nf:
        return 1
    for p in paths:
        p = p.replace("\\", "/")
        if p == nf:
            return 3
        if p.startswith(nf + "/") or nf.startswith(p.rsplit("/", 1)[0] + "/") or p.split("/")[0] == nf.split("/")[0]:
            return 2
    return 0


# Vai nào quan tâm category nào (chỉ để xếp hạng, không lọc bỏ)
_ROLE_CATEGORIES = {"QA": {"test", "bug"}, "DevOps": {"devops", "security"}, "SE": {"bug", "security", "performance", "style"}}


def select_memory(repo: str, paths: list[str], role: str = "", limit: int = 20, pool: int = 200) -> list[dict]:
    """Lớp 3 của tự học: chọn ghi chú LIÊN QUAN tới lô file đang review thay vì 20 ghi chú mới nhất.

    Thứ tự: ghi chú gắn đúng file > cùng thư mục > ghi chú chung của repo > ghi chú về file khác;
    cùng hạng thì ưu tiên ghi chú do người dạy, rồi category hợp vai, rồi mới nhất. Ghi chú về file
    không liên quan chỉ được lấy khi còn chỗ."""
    rows = list_memory(repo, limit=pool)
    if not rows:
        return []
    want = _ROLE_CATEGORIES.get(role, set())
    scored = []
    for i, r in enumerate(rows):            # i nhỏ = mới hơn
        ps = _path_score(r["file"], paths)
        scored.append((ps, 1 if r["source"] == "feedback" else 0, 1 if r["category"] in want else 0, -i, r))
    scored.sort(key=lambda t: t[:4], reverse=True)
    return [t[4] for t in scored[:limit]]


def memory_text(rows: list[dict]) -> str:
    return "\n".join(f"- {'(tự rút ra) ' if r['source'] == 'agent' else ''}{r['note']}" for r in rows)


def memory_for_batch(repo: str, limit: int = 20):
    """Callable truyền vào run_llm_review: mỗi lô file nhận khối memory riêng (xem pipeline)."""
    return lambda paths, role: memory_text(select_memory(repo, paths, role, limit))


def prune_agent_notes(repo: str, keep: int = 30) -> int:
    """Giữ `keep` ghi chú nguồn agent mới nhất của repo, xoá phần cũ hơn. Ghi chú từ người không bị đụng."""
    with Session() as s:
        old = (s.query(RepoMemory).filter_by(repo=repo, source="agent")
               .order_by(RepoMemory.created_at.desc(), RepoMemory.id.desc()).offset(keep).all())
        for r in old:
            s.delete(r)
        s.commit()
        return len(old)


def add_memory(repo: str, note: str, source: str = "feedback", kind: str = "", fingerprint: str = "",
               file: str = "", category: str = ""):
    with Session() as s:
        s.add(RepoMemory(repo=repo, note=note, source=source, kind=kind, fingerprint=fingerprint,
                         file=file, category=category))
        s.commit()


# ---------- posted comments ----------

def get_posted(repo: str, pr_number: int) -> set[str]:
    """Dấu vân tay của mọi comment đã đăng lên PR này ở các lần chạy trước."""
    with Session() as s:
        rows = s.query(PostedComment.fingerprint).filter_by(repo=repo, pr_number=pr_number).all()
    return {r[0] for r in rows}


def save_posted(repo: str, pr_number: int, head_sha: str, items: list):
    """items: dấu vân tay (str) hoặc Finding (có .fingerprint). Với Finding lưu thêm tóm tắt để
    sau này sinh ghi chú memory từ phản hồi mà không cần finding gốc."""
    rows, seen = [], set()
    for it in items:
        fp = it if isinstance(it, str) else it.fingerprint
        if not fp or fp in seen:
            continue
        seen.add(fp)
        row = PostedComment(repo=repo, pr_number=pr_number, fingerprint=fp, head_sha=head_sha)
        if not isinstance(it, str):
            row.file, row.category, row.title = it.file, it.category, it.title[:200]
            row.source, row.severity, row.role = it.source, it.severity, it.role
        rows.append(row)
    if not rows:
        return
    with Session() as s:
        s.add_all(rows)
        s.commit()


def _row_dict(r: PostedComment) -> dict:
    return {"repo": r.repo, "pr_number": r.pr_number, "fingerprint": r.fingerprint, "comment_id": r.comment_id,
            "file": r.file or "", "category": r.category or "", "title": r.title or "",
            "source": r.source or "", "severity": r.severity or "", "role": r.role or ""}


def set_comment_ids(repo: str, pr_number: int, ids_by_fp: dict[str, int]) -> int:
    """Gắn id comment GitHub vào các dấu vân tay vừa đăng (điền cho bản ghi mới nhất chưa có id)."""
    n = 0
    with Session() as s:
        for fp, cid in ids_by_fp.items():
            row = (s.query(PostedComment).filter_by(repo=repo, pr_number=pr_number, fingerprint=fp)
                   .filter(PostedComment.comment_id.is_(None)).order_by(PostedComment.id.desc()).first())
            if row:
                row.comment_id = cid
                n += 1
        s.commit()
    return n


def find_posted_by_comment(comment_id: int) -> dict | None:
    with Session() as s:
        row = s.query(PostedComment).filter_by(comment_id=comment_id).first()
        return _row_dict(row) if row else None


def find_posted_by_fingerprint(repo: str, pr_number: int, fingerprint: str) -> dict | None:
    with Session() as s:
        row = (s.query(PostedComment).filter_by(repo=repo, pr_number=pr_number, fingerprint=fingerprint)
               .order_by(PostedComment.id.desc()).first())
        return _row_dict(row) if row else None


# ---------- feedback ----------

def save_feedback(repo: str, pr_number: int, fingerprint: str, comment_id: int, kind: str, channel: str,
                  author: str = "", body: str = "") -> bool:
    """Ghi một phản hồi; trả False nếu (comment_id, channel, kind) đã có (không ghi lại)."""
    with Session() as s:
        exists = s.query(Feedback.id).filter_by(comment_id=comment_id, channel=channel, kind=kind).first()
        if exists:
            return False
        s.add(Feedback(repo=repo, pr_number=pr_number, fingerprint=fingerprint, comment_id=comment_id,
                       kind=kind, channel=channel, author=author, body=(body or "")[:2000]))
        s.commit()
        return True


def feedback_profile(repo: str) -> list[dict]:
    """Mọi phản hồi của repo kèm tóm tắt finding nó nói về (nối qua fingerprint), cho app.review.calibrate.
    Một phản hồi mà posted_comments không còn bản ghi (DB bị xoá) thì bỏ qua: không biết nó nói về loại gì."""
    with Session() as s:
        fbs = s.query(Feedback.kind, Feedback.fingerprint).filter_by(repo=repo).all()
        if not fbs:
            return []
        fps = {fp for _, fp in fbs}
        posted = {}
        for row in (s.query(PostedComment).filter(PostedComment.repo == repo, PostedComment.fingerprint.in_(fps))
                    .order_by(PostedComment.id.asc())):
            posted[row.fingerprint] = {"file": row.file or "", "category": row.category or "",
                                       "title": row.title or "", "source": row.source or "llm"}
    return [{"kind": kind, **posted[fp]} for kind, fp in fbs if fp in posted]


def list_memory_all(limit: int = 500, kinds: tuple[str, ...] = (), source: str = "") -> list[dict]:
    """Ghi chú của MỌI repo (cho lớp 4: học chéo repo vào checklist), lọc theo kind/source nếu có."""
    with Session() as s:
        q = s.query(RepoMemory)
        if kinds:
            q = q.filter(RepoMemory.kind.in_(kinds))
        if source:
            q = q.filter_by(source=source)
        rows = q.order_by(RepoMemory.created_at.desc(), RepoMemory.id.desc()).limit(limit).all()
        return [{"repo": r.repo, "note": r.note, "source": r.source or "", "kind": r.kind or "",
                 "file": r.file or "", "category": r.category or ""} for r in rows]


def feedback_profile_all(limit: int = 2000) -> list[dict]:
    """Như feedback_profile nhưng gom mọi repo, kèm tên repo."""
    with Session() as s:
        repos = [r[0] for r in s.query(Feedback.repo).distinct().all()]
    out = []
    for repo in repos:
        out += [{"repo": repo, **row} for row in feedback_profile(repo)]
    return out[:limit]


def feedback_stats(repo: str) -> dict[str, int]:
    with Session() as s:
        rows = s.query(Feedback.kind).filter_by(repo=repo).all()
    out = {"accept": 0, "reject": 0}
    for (k,) in rows:
        out[k] = out.get(k, 0) + 1
    return out
