"""Chuẩn bị đầu vào cho trang "Kiểm tra code" (app/demo_sources.py) và các nguồn mới của POST /demo/review.
Phần GitHub được test trên một repo git cục bộ đóng vai remote (không cần mạng)."""
import os
import subprocess
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import demo, demo_sources as ds


# ---------------------------------------------------------------- parse link

@pytest.mark.parametrize("url, want", [
    ("https://github.com/octo/hello", ("octo", "hello", None, None, None)),
    ("https://github.com/octo/hello.git", ("octo", "hello", None, None, None)),
    ("https://github.com/octo/hello/", ("octo", "hello", None, None, None)),
    ("https://github.com/octo/hello/pull/12", ("octo", "hello", 12, None, None)),
    ("https://github.com/octo/hello/pull/12/files?diff=split", ("octo", "hello", 12, None, None)),
    ("https://github.com/octo/hello/tree/feat/x-1", ("octo", "hello", None, "feat/x-1", None)),
    ("http://www.github.com/octo/hello/commit/0123abcd", ("octo", "hello", None, None, "0123abcd")),
])
def test_parse_github_url(url, want):
    t = ds.parse_github_url(url)
    assert (t.owner, t.repo, t.pr, t.branch, t.commit) == want
    assert t.clone_url == "https://github.com/octo/hello.git"


@pytest.mark.parametrize("url", ["", "github.com/octo/hello", "https://gitlab.com/a/b", "https://github.com/octo",
                                 "https://github.com/octo/hello/tree/..", "https://github.com/octo/hello/tree/--bad",
                                 "https://evil.com/github.com/octo/hello"])
def test_parse_github_url_rejects(url):
    with pytest.raises(ds.InputError):
        ds.parse_github_url(url)


def test_github_labels():
    assert ds.parse_github_url("https://github.com/o/r/pull/3").label == "o/r PR #3"
    assert ds.parse_github_url("https://github.com/o/r/tree/dev").label == "o/r nhánh dev"
    assert ds.parse_github_url("https://github.com/o/r").label == "o/r (commit mới nhất)"


# ---------------------------------------------------------------- code / diff dán vào

def test_guess_filename():
    assert ds.guess_filename("def f(x):\n    return x\n") == "code.py"
    assert ds.guess_filename("FROM python:3.12\nRUN pip install x\n") == "Dockerfile"
    assert ds.guess_filename("const a = 1;\nconsole.log(a)\n") == "code.js"
    assert ds.guess_filename("package main\nfunc main() {}\n") == "code.go"
    assert ds.guess_filename("hello world") == "code.txt"


def test_code_to_diff_is_parseable(tmp_path):
    from app.review.diff_utils import parse_diff
    p = ds.prepare_code_text("import os\nos.system('ls ' + x)\n", "", str(tmp_path))
    assert p.head == "code.py" and p.label.endswith("(code.py)")
    assert (tmp_path / "src" / "code.py").read_text(encoding="utf-8") == "import os\nos.system('ls ' + x)\n"
    files = parse_diff(open(p.diff_path, encoding="utf-8").read(), [])
    assert [f.path for f in files] == ["code.py"] and files[0].added_lines == {1, 2}


def test_code_text_validation(tmp_path):
    with pytest.raises(ds.InputError):
        ds.prepare_code_text("   ", "", str(tmp_path))
    with pytest.raises(ds.InputError):
        ds.prepare_code_text("x = 1", "../x.py", str(tmp_path))
    with pytest.raises(ds.InputError):
        ds.prepare_code_text("x" * (ds.MAX_TEXT_CHARS + 1), "", str(tmp_path))


def test_diff_text_validation(tmp_path):
    with pytest.raises(ds.InputError):
        ds.prepare_diff_text("chỉ là văn bản thường", str(tmp_path))
    p = ds.prepare_diff_text("diff --git a/a.py b/a.py\r\n--- a/a.py\r\n+++ b/a.py\r\n@@ -1 +1,2 @@\r\n x = 1\r\n+y = 2", str(tmp_path))
    text = open(p.diff_path, encoding="utf-8").read()
    assert "\r" not in text and text.endswith("+y = 2\n")


# ---------------------------------------------------------------- fetch từ "GitHub" (remote cục bộ)

@pytest.fixture
def remote(tmp_path):
    """Repo git đóng vai github.com/octo/hello: main có 2 commit, nhánh feat thêm 1 commit, refs/pull/7/head = feat."""
    repo = tmp_path / "remote"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "."); git("commit", "-q", "-m", "init")
    (repo / "a.py").write_text("x = 1\nprint(x)\n", encoding="utf-8")
    git("commit", "-q", "-am", "second")
    git("checkout", "-q", "-b", "feat")
    (repo / "a.py").write_text("x = 1\nprint(x)\ny = x / 0\n", encoding="utf-8")
    git("commit", "-q", "-am", "feat")
    git("update-ref", "refs/pull/7/head", "feat")
    git("checkout", "-q", "main")
    return repo


@pytest.fixture
def local_github(remote, monkeypatch):
    monkeypatch.setattr(ds.GitHubTarget, "clone_url", property(lambda self: str(remote)))
    monkeypatch.setattr(ds, "pr_info", lambda t, timeout=10: {})        # không gọi api.github.com
    return remote


def _diff(p: ds.Prepared) -> str:
    return open(p.diff_path, encoding="utf-8").read()


def test_fetch_branch_vs_default(local_github, tmp_path):
    steps = []
    p = ds.fetch_github(ds.parse_github_url("https://github.com/octo/hello/tree/feat"), "", str(tmp_path / "w"), steps.append)
    assert p.base == "main" and p.head == "feat" and "+y = x / 0" in _diff(p) and "+print(x)" not in _diff(p)
    assert (tmp_path / "w" / "src" / "a.py").read_text(encoding="utf-8").endswith("y = x / 0\n")   # đã checkout head
    assert steps and "tải nhánh feat" in steps[0]


def test_fetch_pull_request(local_github, tmp_path):
    p = ds.fetch_github(ds.parse_github_url("https://github.com/octo/hello/pull/7"), "", str(tmp_path / "w"))
    assert p.head == "PR #7" and p.base == "main" and "+y = x / 0" in _diff(p)


def test_fetch_repo_only_reviews_latest_commit(local_github, tmp_path):
    p = ds.fetch_github(ds.parse_github_url("https://github.com/octo/hello"), "", str(tmp_path / "w"))
    assert "+print(x)" in _diff(p) and "y = x / 0" not in _diff(p) and p.base == "commit trước đó"


def test_fetch_rejects_no_change_and_missing_branch(local_github, tmp_path):
    with pytest.raises(ds.InputError, match="Không có thay đổi"):      # PR #7 trỏ vào feat, so với chính feat
        ds.fetch_github(ds.parse_github_url("https://github.com/octo/hello/pull/7"), "feat", str(tmp_path / "w1"))
    with pytest.raises(ds.InputError):
        ds.fetch_github(ds.parse_github_url("https://github.com/octo/hello/tree/khong-co"), "", str(tmp_path / "w2"))


def test_fetch_private_or_missing_repo_message(tmp_path, monkeypatch):
    monkeypatch.setattr(ds.GitHubTarget, "clone_url", property(lambda self: str(tmp_path / "khong-ton-tai")))
    with pytest.raises(ds.InputError):
        ds.fetch_github(ds.parse_github_url("https://github.com/octo/hello/tree/x"), "main", str(tmp_path / "w"))


# ---------------------------------------------------------------- POST /demo/review với nguồn mới

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(demo.demo_settings, "demo_repos", "")
    monkeypatch.setattr(demo.demo_settings, "demo_require_auth", False)
    monkeypatch.setattr(demo.demo_settings, "demo_jobs_dir", str(tmp_path / "jobs"))
    monkeypatch.setattr(demo.demo_settings, "firebase_credentials", "")
    monkeypatch.setattr(demo, "_store", None)
    monkeypatch.setattr(demo, "limiter", demo.RateLimiter(5))
    monkeypatch.setattr(demo, "_running", threading.Semaphore(1))
    calls = []

    def fake_run_job(job_id, repo_name, repo_path, base, head, user, spec=None):
        try:
            calls.append((job_id, repo_name, repo_path, spec))
            demo.get_store().put(job_id, {"status": "done", "findings_count": 2, "report_md": "# bc",
                                          "findings": [{"severity": "high"}, {"severity": "low"}]})
        finally:
            demo._running.release()
    monkeypatch.setattr(demo, "run_job", fake_run_job)
    app = FastAPI()
    demo.install(app)
    c = TestClient(app)
    c.calls = calls
    return c


def test_review_github_source(client):
    r = client.post("/demo/review", json={"source": "github", "url": "https://github.com/octo/hello/pull/7"})
    assert r.status_code == 202, r.text
    job = client.get(f"/demo/jobs/{r.json()['job_id']}").json()
    assert job["source"] == "github" and job["label"] == "octo/hello PR #7" and job["repo"] == "octo/hello"
    assert client.calls[0][2] == "" and client.calls[0][3] == {"source": "github", "url": "https://github.com/octo/hello/pull/7", "base": ""}


def test_review_github_rejects_bad_url_and_base(client):
    assert client.post("/demo/review", json={"source": "github", "url": "https://gitlab.com/a/b"}).status_code == 422
    assert client.post("/demo/review", json={"source": "github", "url": "https://github.com/a/b", "base": "a..b"}).status_code == 422
    assert not client.calls


def test_review_code_and_diff_sources(client):
    r = client.post("/demo/review", json={"source": "code", "text": "x = 1\n", "filename": "a.py"})
    assert r.status_code == 202
    assert client.calls[-1][3] == {"source": "code", "text": "x = 1\n", "filename": "a.py"}
    assert client.get(f"/demo/jobs/{r.json()['job_id']}").json()["label"] == "Đoạn code dán vào (a.py)"
    r = client.post("/demo/review", json={"source": "diff", "text": "diff --git a/x b/x\n"})
    assert r.status_code == 202 and client.calls[-1][3]["source"] == "diff"
    assert client.post("/demo/review", json={"source": "code", "text": "   "}).status_code == 422
    assert client.post("/demo/review", json={"source": "zip", "text": "x"}).status_code == 422


def test_jobs_list_and_report(client):
    r = client.post("/demo/review", json={"source": "code", "text": "x = 1\n"})
    job_id = r.json()["job_id"]
    jobs = client.get("/demo/jobs").json()["jobs"]
    assert jobs[0]["job_id"] == job_id and jobs[0]["by_severity"] == {"high": 1, "low": 1}
    assert "findings" not in jobs[0] and "report_md" not in jobs[0]
    rep = client.get(f"/demo/jobs/{job_id}/report.md")
    assert rep.status_code == 200 and rep.text == "# bc" and "attachment" in rep.headers["content-disposition"]
    assert client.get("/demo/jobs/000000000000/report.md").status_code == 404


def test_run_job_with_spec_prepares_then_runs_cli(tmp_path, monkeypatch):
    """run_job thật với nguồn 'code': phải chuẩn bị diff, truyền --diff-file cho cli và dọn thư mục tạm."""
    import sys
    monkeypatch.setattr(demo.demo_settings, "demo_jobs_dir", str(tmp_path / "jobs"))
    monkeypatch.setattr(demo.demo_settings, "firebase_credentials", "")
    monkeypatch.setattr(demo, "_store", None)
    monkeypatch.setattr(demo, "_running", threading.Semaphore(0))
    seen = {}

    def fake_cli(repo, base, head, out_dir):
        seen.update(repo=repo, base=base, head=head)
        script = ("import json, os, sys\nout, diff = sys.argv[1], sys.argv[3]\n"
                  "assert sys.argv[2] == '--diff-file' and os.path.exists(diff)\n"
                  "json.dump([], open(os.path.join(out, 'findings.json'), 'w'))\n"
                  "open(os.path.join(out, 'report.md'), 'w').write('ok')\nprint('0 findings -> r', flush=True)\n")
        return [sys.executable, "-c", script, out_dir]
    monkeypatch.setattr(demo, "_cli_cmd", fake_cli)
    demo.run_job("abcdefabcdef", "", "", "", "", {"uid": "u", "name": "n"}, {"source": "code", "text": "x = 1\n", "filename": "m.py"})
    job = demo.get_store().get("abcdefabcdef")
    assert job["status"] == "done" and job["label"] == "Đoạn code dán vào (m.py)" and job["head"] == "m.py"
    assert [s["step"] for s in job["steps"]][0] == "fetch" and job["steps"][-1]["step"] == "done"
    assert seen["head"] == "m.py" and not os.path.exists(seen["repo"])     # thư mục tạm đã dọn
    assert demo._running.acquire(blocking=False)


def test_run_job_with_bad_input_fails_gently(tmp_path, monkeypatch):
    monkeypatch.setattr(demo.demo_settings, "demo_jobs_dir", str(tmp_path / "jobs"))
    monkeypatch.setattr(demo.demo_settings, "firebase_credentials", "")
    monkeypatch.setattr(demo, "_store", None)
    monkeypatch.setattr(demo, "_running", threading.Semaphore(0))
    monkeypatch.setattr(demo, "_cli_cmd", lambda *a: (_ for _ in ()).throw(AssertionError("không được gọi cli")))
    demo.run_job("0123456789ab", "", "", "", "", {"uid": "u", "name": "n"}, {"source": "diff", "text": "văn bản thường"})
    job = demo.get_store().get("0123456789ab")
    assert job["status"] == "failed" and "định dạng diff" in job["error"]
    assert demo._running.acquire(blocking=False)

