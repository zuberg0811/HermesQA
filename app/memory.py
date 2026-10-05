"""Bộ nhớ theo repo: convention/feedback học được, và dấu vân tay comment đã đăng (để không đăng lại).
MVP dùng Postgres qua SQLAlchemy; Hermes Agent có memory riêng nhưng ta giữ 1 lớp độc lập để không lock-in."""
import logging
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, JSON
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
    source = Column(String)             # feedback | agent
    created_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(engine)


def save_run(repo: str, pr_number: int, head_sha: str, roles: list, findings: list, conclusion: str):
    with Session() as s:
        s.add(ReviewRun(repo=repo, pr_number=pr_number, head_sha=head_sha, roles=roles,
                        findings=[f.model_dump() for f in findings], conclusion=conclusion))
        s.commit()


def get_repo_memory(repo: str, limit: int = 20) -> str:
    with Session() as s:
        rows = s.query(RepoMemory).filter_by(repo=repo).order_by(RepoMemory.created_at.desc()).limit(limit).all()
    return "\n".join(f"- {r.note}" for r in rows)


def add_memory(repo: str, note: str, source: str = "feedback"):
    with Session() as s:
        s.add(RepoMemory(repo=repo, note=note, source=source))
        s.commit()


class PostedComment(Base):
    """Dấu vân tay của mỗi comment inline đã đăng lên một PR (xem postprocess.fingerprint)."""
    __tablename__ = "posted_comments"
    id = Column(Integer, primary_key=True)
    repo = Column(String, index=True)
    pr_number = Column(Integer, index=True)
    fingerprint = Column(String, index=True)
    head_sha = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


def get_posted(repo: str, pr_number: int) -> set[str]:
    """Dấu vân tay của mọi comment đã đăng lên PR này ở các lần chạy trước."""
    with Session() as s:
        rows = s.query(PostedComment.fingerprint).filter_by(repo=repo, pr_number=pr_number).all()
    return {r[0] for r in rows}


def save_posted(repo: str, pr_number: int, head_sha: str, fingerprints: list[str]):
    if not fingerprints:
        return
    with Session() as s:
        s.add_all(PostedComment(repo=repo, pr_number=pr_number, fingerprint=fp, head_sha=head_sha)
                  for fp in dict.fromkeys(fingerprints))
        s.commit()
