"""Endpoint "Review thử" của website demo (app/demo.py): hàng rào repo mẫu / nhánh / tần suất / một-lúc-một,
và việc đọc log của app.cli thành tiến độ. Không gọi LLM, không cần Firebase: job ghi file cục bộ."""
import subprocess
import sys
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import demo


@pytest.fixture
def sample_repo(tmp_path):
    """Repo git nhỏ có nhánh main và feat (để /demo/repos liệt kê nhánh)."""
    repo = tmp_path / "mau"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "."); git("commit", "-q", "-m", "init")
    git("checkout", "-q", "-b", "feat")
    (repo / "a.py").write_text("x = 1\ny = x / 0\n", encoding="utf-8")
    git("commit", "-q", "-am", "feat")
    git("checkout", "-q", "main")
    return repo


@pytest.fixture
def client(tmp_path, sample_repo, monkeypatch):
    monkeypatch.setattr(demo.demo_settings, "demo_repos", f"mau={sample_repo}, hong=/khong/ton/tai")
    monkeypatch.setattr(demo.demo_settings, "demo_require_auth", False)
    monkeypatch.setattr(demo.demo_settings, "demo_jobs_dir", str(tmp_path / "jobs"))
    monkeypatch.setattr(demo.demo_settings, "firebase_credentials", "")
    monkeypatch.setattr(demo, "_store", None)
    monkeypatch.setattr(demo, "limiter", demo.RateLimiter(2))
    monkeypatch.setattr(demo, "_running", threading.Semaphore(1))
    calls = []

    def fake_run_job(job_id, repo_name, repo_path, base, head, user):   # không chạy subprocess thật
        try:
            calls.append((job_id, repo_name, repo_path, base, head))
            demo.get_store().put(job_id, {"status": "done", "findings_count": 1})
        finally:
            demo._running.release()
    monkeypatch.setattr(demo, "run_job", fake_run_job)
    app = FastAPI()
    demo.install(app)
    c = TestClient(app)
    c.calls = calls
    return c


def test_parse_repos_ignores_missing_paths(sample_repo):
    repos = demo.parse_repos(f"mau={sample_repo}, hong=/khong/ton/tai, khongdau, =rong")
    assert repos == {"mau": str(sample_repo)}


def test_repos_endpoint_lists_branches(client):
    data = client.get("/demo/repos").json()
    assert [r["name"] for r in data["repos"]] == ["mau"]
    assert set(data["repos"][0]["branches"]) == {"main", "feat"}
    assert data["require_auth"] is False and data["store"] == "local"


def test_review_rejects_unknown_repo_and_bad_refs(client):
    assert client.post("/demo/review", json={"repo": "khac", "head": "feat"}).status_code == 404
    assert client.post("/demo/review", json={"repo": "hong", "head": "feat"}).status_code == 404   # đường dẫn không tồn tại
    for bad in ("--upload-pack=x", "a..b", "feat;rm -rf", "", "x" * 130):
        r = client.post("/demo/review", json={"repo": "mau", "head": bad})
        assert r.status_code == 422, bad
    assert not client.calls


def test_review_queues_job_then_rate_limits(client):
    r = client.post("/demo/review", json={"repo": "mau", "head": "feat"})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    job = client.get(f"/demo/jobs/{job_id}").json()
    assert job["status"] == "done" and job["repo"] == "mau" and job["head"] == "feat"   # background đã chạy xong
    assert job["user"] == "khách" and job["uid"].startswith("anon:")
    assert client.calls and client.calls[0][0] == job_id
    assert client.post("/demo/review", json={"repo": "mau", "head": "feat"}).status_code == 202
    r3 = client.post("/demo/review", json={"repo": "mau", "head": "feat"})
    assert r3.status_code == 429 and "Retry-After" in r3.headers
    assert len(client.calls) == 2


def test_review_refuses_when_busy(client):
    assert demo._running.acquire(blocking=False)
    try:
        assert client.post("/demo/review", json={"repo": "mau", "head": "feat"}).status_code == 409
    finally:
        demo._running.release()
    assert not client.calls


def test_review_requires_token_when_auth_on(client, monkeypatch):
    monkeypatch.setattr(demo.demo_settings, "demo_require_auth", True)
    assert client.post("/demo/review", json={"repo": "mau", "head": "feat"}).status_code == 401
    assert client.post("/demo/review", json={"repo": "mau", "head": "feat"}, headers={"Authorization": "Basic x"}).status_code == 401


def test_jobs_endpoint_404_for_unknown(client):
    assert client.get("/demo/jobs/000000000000").status_code == 404
    assert client.get("/demo/jobs/..%2F..%2Fetc").status_code == 404


def test_classify_cli_lines():
    assert demo.classify_line("[agent] SE ...") == ("llm:SE", "đang hỏi vai SE")
    assert demo.classify_line("[agent] QA LỖI: (lỗi khi chạy: 429)") == ("failed:QA", "vai QA lỗi")
    assert demo.classify_line("INFO hermesqa.sandbox: container xong") == ("static", "quét công cụ tĩnh trong sandbox")
    assert demo.classify_line("3 findings -> report.md") == ("done", "3 nhận xét")
    assert demo.classify_line("INFO hermesqa.agent: [SE] tokens in=1200 out=300") == (None, "")


def test_run_job_streams_progress_from_subprocess(tmp_path, monkeypatch):
    """run_job thật, nhưng thay app.cli bằng một script in đúng các dòng log cli hay in."""
    monkeypatch.setattr(demo.demo_settings, "demo_jobs_dir", str(tmp_path / "jobs"))
    monkeypatch.setattr(demo.demo_settings, "firebase_credentials", "")
    monkeypatch.setattr(demo.demo_settings, "demo_timeout", 60)
    monkeypatch.setattr(demo, "_store", None)
    monkeypatch.setattr(demo, "_running", threading.Semaphore(0))     # run_job sẽ release
    script = (
        "import json, os, sys\n"
        "out = sys.argv[1]\n"
        "print('[agent] SE ...', flush=True)\n"
        "print('INFO hermesqa.agent: [SE] tokens in=100 out=20', file=sys.stderr, flush=True)\n"
        "print('[agent] QA ...', flush=True)\n"
        "print('INFO hermesqa.agent: [QA] tokens in=50 out=5', file=sys.stderr, flush=True)\n"
        "json.dump([{'file': 'a.py', 'line': 2, 'severity': 'high', 'category': 'bug', 'title': 'chia 0', 'explanation': 'x'}],"
        " open(os.path.join(out, 'findings.json'), 'w'))\n"
        "open(os.path.join(out, 'report.md'), 'w').write('# bao cao')\n"
        "print('1 findings -> report.md', flush=True)\n"
        "assert os.environ['HERMESQA_RUN_ID'] == 'abcdefabcdef'\n"
    )
    monkeypatch.setattr(demo, "_cli_cmd", lambda repo, base, head, out_dir: [sys.executable, "-c", script, out_dir])
    demo.run_job("abcdefabcdef", "mau", str(tmp_path), "main", "feat", {"uid": "u", "name": "n"})
    job = demo.get_store().get("abcdefabcdef")
    assert job["status"] == "done" and job["returncode"] == 0 and job["error"] == ""
    assert [s["step"] for s in job["steps"]] == ["llm:SE", "llm:QA", "done"]
    assert job["tokens_in"] == 150 and job["tokens_out"] == 25
    assert job["findings_count"] == 1 and job["findings"][0]["title"] == "chia 0"
    assert job["report_md"] == "# bao cao"
    assert demo._running.acquire(blocking=False)      # đã trả lại chỗ chạy


def test_run_job_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(demo.demo_settings, "demo_jobs_dir", str(tmp_path / "jobs"))
    monkeypatch.setattr(demo.demo_settings, "firebase_credentials", "")
    monkeypatch.setattr(demo, "_store", None)
    monkeypatch.setattr(demo, "_running", threading.Semaphore(0))
    monkeypatch.setattr(demo, "_cli_cmd", lambda *a: [sys.executable, "-c", "import sys; print('boom'); sys.exit(1)"])
    demo.run_job("0123456789ab", "mau", str(tmp_path), "main", "feat", {"uid": "u", "name": "n"})
    job = demo.get_store().get("0123456789ab")
    assert job["status"] == "failed" and "boom" in job["error"]
    assert demo._running.acquire(blocking=False)


def test_rate_limiter_window():
    rl = demo.RateLimiter(2)
    assert rl.retry_after("u", now=1000) == 0
    rl.hit("u", now=1000); rl.hit("u", now=1100)
    assert 0 < rl.retry_after("u", now=1200) <= 3400
    assert rl.retry_after("u", now=1000 + 3601) == 0          # lượt đầu đã ra khỏi cửa sổ 1 giờ
    assert rl.retry_after("khac", now=1200) == 0
