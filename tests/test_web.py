"""Test cho dashboard (app/web) và run log (app/runlog): điểm theo agent, lưu/đọc nhật ký, các trang render.

Nguyên tắc như test_improvements: đi qua đúng đường chạy thật (TestClient trên app.main, CLI ghi log qua
record_run) thay vì chỉ test hàm lẻ.
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
import score  # noqa: E402  (eval/score.py)

from app import runlog  # noqa: E402
from app.review.pipeline import LLMReview  # noqa: E402
from app.review.schemas import Finding  # noqa: E402


def _f(file="a.py", line=5, cat="bug", role="SE", source="llm", title="t"):
    return Finding(file=file, line=line, severity="medium", category=cat, title=title, explanation="e",
                   source=source, role=role)


# ---------- score.py: điểm theo agent ----------

def test_score_dir_counts_per_agent(tmp_path):
    gt = {"cases": {"c1": {"truth": [{"file": "a.py", "line": 5, "categories": ["bug"], "note": "n"},
                                     {"file": "b.py", "line": 9, "categories": ["security"], "note": "m"}]}}}
    out = tmp_path / "both"
    out.mkdir()
    findings = [
        {"file": "a.py", "line": 6, "category": "bug", "source": "llm", "role": "SE"},         # TP của SE
        {"file": "a.py", "line": 30, "category": "test", "source": "llm", "role": "QA"},       # FP của QA
        {"file": "b.py", "line": 9, "category": "security", "source": "semgrep"},               # TP của semgrep
        {"file": "b.py", "line": 9, "category": "security", "source": "bandit"},                # trùng -> FP của bandit
    ]
    (out / "c1.json").write_text(json.dumps(findings), encoding="utf-8")
    (tmp_path / "runs_both.json").write_text(json.dumps([{"case": "c1", "returncode": 0, "seconds": 2.0,
                                                           "tokens_in": 10, "tokens_out": 5}]), encoding="utf-8")
    res = score.score_dir(gt, str(tmp_path), 3)
    s = res["configs"]["both"]
    assert (s["tp"], s["fp"], s["fn"]) == (2, 2, 0)
    assert s["by_agent"]["SE"] == {"tp": 1, "fp": 0, "findings": 1, "precision": 1.0}
    assert s["by_agent"]["QA"]["fp"] == 1 and s["by_agent"]["bandit"]["fp"] == 1 and s["by_agent"]["semgrep"]["tp"] == 1
    assert s["avg_tokens"] == 15
    md = score.render_markdown(res)
    assert "| SE | 1 | 1 | 0 | 100.0% |" in md and "## Tổng hợp" in md


# ---------- runlog: ghi và đọc ----------

def _review():
    res = LLMReview()
    res.role_stats["SE"].update({"calls": 1, "raw": 2, "kept": 2, "tokens_in": 100, "tokens_out": 10})
    res.role_stats["QA"].update({"calls": 1, "raw": 1, "kept": 0, "removed": 1, "tokens_in": 80, "tokens_out": 5})
    res.stats.update({"llm_calls": 2, "tokens_in": 180, "tokens_out": 15})
    res.removed.append({"role": "QA", "file": "a.py", "line": 7, "title": "x", "ground": "B", "check": "ok"})
    res.summaries = {"SE": "ổn", "QA": "thiếu test"}
    return res


def test_run_record_and_local_store_roundtrip(tmp_path):
    final = [_f(role="SE"), _f(line=9, role="SE", title="u"), _f(file="b.py", source="semgrep", role="", title="s")]
    rec = runlog.build_run_record(source="cli", pr_meta={"owner": "o", "repo": "r", "pr_number": 3, "title": "T", "head_sha": "abc"},
                                  roles=["SE", "QA"], res=_review(), static=[final[2]], final=final, tools=None,
                                  seconds=12.34, model="m", verify_mode="factcheck", skip_static=False)
    assert rec["role_stats"]["SE"]["final"] == 2 and rec["role_stats"]["QA"]["final"] == 0
    assert rec["counts"] == {"static": 1, "llm_raw": 3, "llm_kept": 2, "final": 3,
                             "by_severity": {"medium": 3}, "by_category": {"bug": 3}}
    assert rec["static_by_tool"] == {"semgrep": 1} and rec["seconds"] == 12.3 and rec["repo"] == "o/r"
    json.dumps(rec)                                                     # phải JSON được (Firestore/file)

    store = runlog.LocalJsonStore(str(tmp_path / "runs"))
    rid = store.save(rec)
    lst = store.list_runs()
    assert len(lst) == 1 and lst[0]["run_id"] == rid and "findings" not in lst[0]   # danh sách là bản rút gọn
    full = store.get(rid)
    assert full["findings"][0]["title"] == "t" and store.get("khong-co") is None


def test_get_store_falls_back_to_local_when_firestore_unavailable(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "runlog_backend", "firestore")
    monkeypatch.setattr(settings, "firebase_credentials", str(tmp_path / "khong-ton-tai.json"))
    monkeypatch.setattr(settings, "runlog_dir", str(tmp_path / "runs"))
    monkeypatch.setattr(runlog, "_store", None)
    assert isinstance(runlog.get_store(), runlog.LocalJsonStore)       # credentials hỏng không được làm hỏng review
    monkeypatch.setattr(runlog, "_store", None)


# ---------- dashboard: các trang render trên dữ liệu thật của repo ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "runlog_backend", "local")
    monkeypatch.setattr(settings, "runlog_dir", str(tmp_path / "runs"))
    monkeypatch.setattr(runlog, "_store", None)
    from app.main import app
    yield TestClient(app)
    monkeypatch.setattr(runlog, "_store", None)


def test_dashboard_pages_render(client):
    for path in ("/dashboard", "/dashboard/agents", "/dashboard/runs"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "HermesQA" in r.text
    assert client.get("/dashboard/runs/nope").status_code == 404
    assert client.get("/dashboard/api/runs").json() == []


def test_dashboard_shows_recorded_run_per_role(client):
    final = [_f(role="SE", title="Chia cho 0"), _f(line=9, role="QA", cat="test", title="Thiếu test")]
    rid = runlog.record_run(source="cli", pr_meta={"owner": "o", "repo": "r", "pr_number": 0, "title": "main..HEAD"},
                            roles=["SE", "QA"], res=_review(), static=[], final=final, tools=None,
                            seconds=3.0, model="m", verify_mode="factcheck", skip_static=True)
    assert rid
    runs = client.get("/dashboard/runs").text
    assert "main..HEAD" in runs and f"/dashboard/runs/{rid}" in runs
    detail = client.get(f"/dashboard/runs/{rid}").text
    assert "Chia cho 0" in detail and "Thiếu test" in detail and "thiếu test" in detail   # finding + tóm tắt vai
    assert "Bị loại sau khi sinh" in detail
    agents = client.get("/dashboard/agents").text
    assert "1 lần chạy gần nhất" in agents
    # HTMX: yêu cầu partial chỉ trả bảng, không có khung trang
    part = client.get("/dashboard/runs", headers={"HX-Request": "true"}).text
    assert "<html" not in part and f"/dashboard/runs/{rid}" in part


# ---------- RunContext: tiến độ từng bước, id ép từ môi trường ----------

def test_run_context_writes_progress_then_full_record(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "runlog_backend", "local")
    monkeypatch.setattr(settings, "runlog_dir", str(tmp_path / "runs"))
    monkeypatch.setattr(runlog, "_store", None)
    monkeypatch.setenv("HERMESQA_RUN_ID", "job-123")          # web demo và run log dùng chung id

    ctx = runlog.RunContext("cli", {"owner": "o", "repo": "r", "title": "T"}, "m")
    assert ctx.run_id == "job-123"
    running = runlog.get_store().get("job-123")
    assert running["status"] == "running" and running["current"] == "start" and running["steps"] == []

    ctx.step("static", "quét")
    ctx.step("SE:start", "2 file")
    mid = runlog.get_store().get("job-123")
    assert mid["current"] == "SE:start" and [s["step"] for s in mid["steps"]] == ["static", "SE:start"]

    final = [_f(role="SE", title="x")]
    rid = ctx.finish(source="cli", pr_meta={"owner": "o", "repo": "r", "title": "T"}, roles=["SE"], res=_review(),
                     static=[], final=final, tools=None, model="m", verify_mode="factcheck", skip_static=True)
    done = runlog.get_store().get(rid)
    assert done["status"] == "done" and done["current"] == "done" and done["steps"][-1]["step"] == "done"
    assert done["findings"][0]["title"] == "x" and done["role_stats"]["SE"]["final"] == 1
    assert len(runlog.get_store().list_runs()) == 1                       # vẫn một document, không nhân đôi

    ctx2 = runlog.RunContext("worker", {"owner": "o", "repo": "r"}, "m")
    monkeypatch.delenv("HERMESQA_RUN_ID")
    ctx2.fail("nổ")
    assert runlog.get_store().get(ctx2.run_id)["status"] == "failed"
    monkeypatch.setattr(runlog, "_store", None)
