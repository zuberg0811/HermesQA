"""Lớp 1 của tự học: phản hồi của người (reply / 👍👎) -> repo memory -> prompt lần sau.

Đi qua đường chạy thật: bảng SQLite trong bộ nhớ (conftest), to_github_comment thật, webhook thật
(TestClient trên app.main, chỉ thay queue.enqueue), GitHub client thay bằng bản giả ghi lại lời gọi."""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import feedback, memory
from app.review import postprocess
from app.review.schemas import Finding


def _f(file="scripts/run.py", line=3, sev="medium", cat="style", title="Dùng print() thay logging", code="print(x)",
       source="llm", role="SE"):
    return Finding(file=file, line=line, severity=sev, category=cat, title=title, explanation="e",
                   existing_code=code, source=source, role=role)


class FakeGH:
    """Giả GitHubClient: chỉ hai hàm đọc comment mà app.feedback cần."""
    def __init__(self, comments=None):
        self.comments = comments or []
        self.calls = []

    def list_review_comments(self, owner, repo, pr):
        self.calls.append(("list", owner, repo, pr))
        return self.comments

    def get_review_comment(self, owner, repo, comment_id):
        self.calls.append(("get", comment_id))
        for c in self.comments:
            if c["id"] == comment_id:
                return c
        raise RuntimeError("404")


@pytest.fixture(autouse=True)
def _db():
    memory.init_db()
    yield
    memory.Base.metadata.drop_all(memory.engine)


# ---------- phân loại ----------

@pytest.mark.parametrize("text,kind", [
    ("👎", "reject"), ("-1", "reject"), ("false positive", "reject"), ("Cái này không phải lỗi, team cố ý", "reject"),
    ("bỏ qua nhé", "reject"), ("by design", "reject"), ("wontfix", "reject"),
    ("👍", "accept"), ("+1", "accept"), ("good catch, will fix", "accept"), ("đúng rồi, đã sửa", "accept"),
    ("Cảm ơn bot", "accept"),
    ("", None), ("hmm?", None), ("dùng fpdf ở đây", None),   # 'fp' không được ăn vào 'fpdf'
    ("không đúng đâu", "reject"),                             # reject thắng accept khi cùng xuất hiện
])
def test_classify(text, kind):
    assert feedback.classify(text) == kind


def test_classify_reactions():
    assert feedback.classify_reactions(None) == []
    assert feedback.classify_reactions({"+1": 0, "-1": 0}) == []
    assert feedback.classify_reactions({"+1": 2, "-1": 1}) == ["reject", "accept"]


# ---------- dấu ẩn trong comment ----------

def test_github_comment_carries_hidden_fingerprint():
    f = _f()
    c = postprocess.to_github_comment(f)
    assert f.fingerprint and feedback.fingerprint_from_body(c["body"]) == f.fingerprint
    assert "<!-- hermesqa:fp=" in c["body"] and "👍/👎" in c["body"]
    assert feedback.fingerprint_from_body("no marker here") is None


# ---------- lưu finding đã đăng + gắn id comment ----------

def test_save_posted_keeps_summary_and_links_comment_ids():
    f1, f2 = _f(), _f(file="app/x.py", cat="bug", title="chia cho 0", code="a/b")
    for f in (f1, f2):
        postprocess.to_github_comment(f)           # gán fingerprint như worker làm
    memory.save_posted("o/r", 7, "sha", [f1, f2])
    assert memory.get_posted("o/r", 7) == {f1.fingerprint, f2.fingerprint}

    gh = FakeGH([
        {"id": 101, "pull_request_review_id": 55, "body": postprocess.to_github_comment(f1)["body"], "path": f1.file},
        {"id": 102, "pull_request_review_id": 55, "body": postprocess.to_github_comment(f2)["body"], "path": f2.file},
        {"id": 103, "pull_request_review_id": 55, "body": "comment của người, không có dấu", "path": "a"},
        {"id": 104, "pull_request_review_id": 11, "body": f"<!-- hermesqa:fp={f1.fingerprint} -->", "path": "cũ"},  # review khác
    ])
    assert feedback.link_comment_ids(gh, "o", "r", 7, 55) == 2
    p = memory.find_posted_by_comment(101)
    assert p and p["fingerprint"] == f1.fingerprint and p["file"] == "scripts/run.py" and p["category"] == "style"
    assert p["title"] == "Dùng print() thay logging"
    assert memory.find_posted_by_comment(104) is None


# ---------- ghi phản hồi -> memory ----------

def test_record_writes_memory_once_and_prompt_sees_it():
    f = _f(); postprocess.to_github_comment(f)
    memory.save_posted("o/r", 7, "sha", [f])
    memory.set_comment_ids("o/r", 7, {f.fingerprint: 101})
    posted = memory.find_posted_by_comment(101)

    assert feedback.record(posted, "reject", "reply", "alice", "không phải lỗi, scripts dùng print là chuẩn")
    assert not feedback.record(posted, "reject", "reply", "alice", "lặp lại")        # cùng comment, cùng kind: không ghi 2 lần
    assert feedback.record(posted, "accept", "reaction")                            # kind khác thì vẫn ghi

    mem = memory.get_repo_memory("o/r")
    assert "KHÔNG báo lại: `scripts/run.py` — [style] Dùng print() thay logging" in mem
    assert "scripts dùng print là chuẩn" in mem
    assert "Đã xác nhận ĐÚNG" in mem and "(reaction 👍)" in mem
    assert memory.feedback_stats("o/r") == {"accept": 1, "reject": 1}
    assert memory.get_repo_memory("khac/repo") == ""                                # memory tách theo repo


def test_security_reject_note_is_advisory_only():
    posted = {"repo": "o/r", "pr_number": 1, "fingerprint": "abcdef0123456789", "comment_id": 9,
              "file": "app/db.py", "category": "security", "title": "SQL injection", "source": "semgrep"}
    note = feedback.build_note(posted, "reject", "đây là query nội bộ")
    assert "(luật semgrep)" in note and "Bảo mật: chỉ tham khảo" in note


# ---------- reply qua webhook -> worker.ingest_feedback -> ingest_reply ----------

def _reply(body, parent=101, login="alice", utype="User", cid=500):
    return {"id": cid, "in_reply_to_id": parent, "body": body, "user": {"login": login, "type": utype}}


def test_ingest_reply_resolves_parent_from_db():
    f = _f(); postprocess.to_github_comment(f)
    memory.save_posted("o/r", 7, "sha", [f])
    memory.set_comment_ids("o/r", 7, {f.fingerprint: 101})
    gh = FakeGH()
    assert feedback.ingest_reply(gh, "o", "r", 7, _reply("bỏ qua, cố ý")) == "reject"
    assert gh.calls == []                                                           # không cần gọi GitHub
    assert "KHÔNG báo lại" in memory.get_repo_memory("o/r")


def test_ingest_reply_falls_back_to_hidden_marker_when_db_has_no_id():
    f = _f(); body = postprocess.to_github_comment(f)["body"]
    memory.save_posted("o/r", 7, "sha", [f])                                         # chưa gắn comment_id
    gh = FakeGH([{"id": 101, "body": body, "path": f.file}])
    assert feedback.ingest_reply(gh, "o", "r", 7, _reply("👍 đúng rồi")) == "accept"
    assert ("get", 101) in gh.calls
    assert memory.find_posted_by_comment(101)["fingerprint"] == f.fingerprint      # id được gắn lại cho lần sau


def test_ingest_reply_ignores_bot_unclassified_and_foreign_comments():
    f = _f(); body = postprocess.to_github_comment(f)["body"]
    memory.save_posted("o/r", 7, "sha", [f])
    gh = FakeGH([{"id": 101, "body": body, "path": f.file}, {"id": 200, "body": "comment người thường", "path": "x"}])
    assert feedback.ingest_reply(gh, "o", "r", 7, _reply("👎", utype="Bot")) is None
    assert feedback.ingest_reply(gh, "o", "r", 7, _reply("hmm?")) is None
    assert feedback.ingest_reply(gh, "o", "r", 7, _reply("👎", parent=200)) is None   # trả lời vào comment không phải của bot
    assert feedback.ingest_reply(gh, "o", "r", 7, {"id": 1, "body": "👎", "user": {}}) is None  # không phải reply
    assert memory.get_repo_memory("o/r") == ""


# ---------- 👍/👎 đọc lại ở lần push sau ----------

def test_sync_reactions_records_each_reaction_once():
    f1, f2 = _f(), _f(file="app/x.py", cat="bug", title="chia cho 0", code="a/b")
    b1, b2 = postprocess.to_github_comment(f1)["body"], postprocess.to_github_comment(f2)["body"]
    memory.save_posted("o/r", 7, "sha", [f1, f2])
    gh = FakeGH([
        {"id": 101, "body": b1, "path": f1.file, "reactions": {"+1": 0, "-1": 2}},
        {"id": 102, "body": b2, "path": f2.file, "reactions": {"+1": 1, "-1": 0}},
        {"id": 103, "body": "người khác", "path": "a", "reactions": {"-1": 5}},     # không phải của bot
    ])
    assert feedback.sync_reactions(gh, "o", "r", 7) == 2
    assert feedback.sync_reactions(gh, "o", "r", 7) == 0                            # lần push sau: không ghi lại
    mem = memory.get_repo_memory("o/r")
    assert "KHÔNG báo lại: `scripts/run.py`" in mem and "(reaction 👎)" in mem
    assert "Đã xác nhận ĐÚNG: `app/x.py`" in mem
    assert memory.feedback_stats("o/r") == {"accept": 1, "reject": 1}


def test_sync_reactions_survives_github_error():
    class Broken:
        def list_review_comments(self, *a):
            raise RuntimeError("rate limited")
    assert feedback.sync_reactions(Broken(), "o", "r", 7) == 0


# ---------- webhook: pull_request_review_comment -> job ingest_feedback ----------

@pytest.fixture
def client(monkeypatch):
    from app import main
    queued = []

    class _Job:
        id = "job-1"

    monkeypatch.setattr(main.queue, "enqueue", lambda fn, payload, **kw: queued.append((fn, payload)) or _Job())
    return TestClient(main.app), queued


def _post(client, event, payload):
    from app.config import settings
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(settings.github_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhook/github", content=body,
                       headers={"X-GitHub-Event": event, "X-Hub-Signature-256": sig, "X-GitHub-Delivery": "d1",
                                "Content-Type": "application/json"})


def _payload(action="created", in_reply_to=101, utype="User"):
    c = {"id": 500, "body": "👎 không phải lỗi", "user": {"login": "alice", "type": utype}}
    if in_reply_to:
        c["in_reply_to_id"] = in_reply_to
    return {"action": action, "comment": c, "installation": {"id": 1},
            "repository": {"name": "r", "owner": {"login": "o"}, "clone_url": "https://x/o/r.git"},
            "pull_request": {"number": 7}}


def test_webhook_queues_reply_to_bot_comment(client):
    c, queued = client
    r = _post(c, "pull_request_review_comment", _payload())
    assert r.status_code == 200 and r.json()["msg"] == "queued feedback"
    fn, payload = queued[0]
    assert fn == "app.worker.ingest_feedback"
    assert payload["owner"] == "o" and payload["pr_number"] == 7
    assert payload["comment"]["in_reply_to_id"] == 101 and payload["comment"]["body"] == "👎 không phải lỗi"


def test_webhook_ignores_top_level_bot_and_edited_comments(client):
    c, queued = client
    assert "ignored" in _post(c, "pull_request_review_comment", _payload(in_reply_to=None)).json()["msg"]
    assert "ignored" in _post(c, "pull_request_review_comment", _payload(utype="Bot")).json()["msg"]
    assert "ignored" in _post(c, "pull_request_review_comment", _payload(action="edited")).json()["msg"]
    assert queued == []


def test_webhook_rejects_bad_signature(client):
    c, _ = client
    r = c.post("/webhook/github", content=b"{}", headers={"X-GitHub-Event": "pull_request_review_comment",
                                                           "X-Hub-Signature-256": "sha256=deadbeef"})
    assert r.status_code == 401


# ---------- DB cũ (trước khi có feedback) được bổ sung cột ----------

def test_init_db_adds_missing_columns_to_old_table():
    from sqlalchemy import text, inspect
    memory.Base.metadata.drop_all(memory.engine)
    with memory.engine.begin() as conn:
        conn.execute(text("CREATE TABLE posted_comments (id INTEGER PRIMARY KEY, repo VARCHAR, pr_number INTEGER, "
                          "fingerprint VARCHAR, head_sha VARCHAR, created_at DATETIME)"))
    memory.init_db()
    cols = {c["name"] for c in inspect(memory.engine).get_columns("posted_comments")}
    assert {"comment_id", "file", "category", "title"} <= cols
    memory.save_posted("o/r", 1, "s", [_f()])                                       # ghi được vào bảng đã nâng cấp
