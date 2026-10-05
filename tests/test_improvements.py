"""Test cho 7 cải tiến lấy ý tưởng từ open-code-review: ghim bằng existing_code, kiểm tra sự thật,
checklist theo loại file, chia lô không cắt im lặng, chặn secret, dấu vân tay comment.

Nguyên tắc rút ra từ bug D3: test phải đi qua ĐÚNG ĐƯỜNG pipeline gọi (run_llm_review), không chỉ
test từng hàm lẻ — một hàm có test riêng vẫn có thể chưa bao giờ được gọi.
"""
import pytest

from app.config import settings
from app.review import postprocess, report
from app.review.agent import fact_check, parse_output, is_protected
from app.review.diff_utils import anchor_finding, file_context, parse_diff, render_patch, resolve_anchor, REDACTED
from app.review.pipeline import run_llm_review
from app.review.router import select_roles
from app.review.rules import resolve_rules, rule_files_for
from app.review.schemas import AgentOutput, Finding
from app.review.selection import (Skipped, TRUNCATION_MARK, build_batches, files_for_role,
                                  is_generated_path, is_secret_path, select_for_llm)

SECRET = "AKIAIOSFODNN7EXAMPLE"

DIFF = "\n".join([
    "diff --git a/app/svc.py b/app/svc.py",
    "--- a/app/svc.py",
    "+++ b/app/svc.py",
    "@@ -1,3 +1,9 @@",
    " import os",
    " ",
    "+def avg(xs):",
    "+    if not xs:",
    "+        return 0",
    "+    return sum(xs) / len(xs)",
    "+",
    f'+API_KEY = "{SECRET}"',
    " def old():",
    "diff --git a/.env b/.env",
    "--- a/.env",
    "+++ b/.env",
    "@@ -1,1 +1,2 @@",
    " DEBUG=1",
    "+TOKEN=super-secret-value",
    "diff --git a/package-lock.json b/package-lock.json",
    "--- a/package-lock.json",
    "+++ b/package-lock.json",
    "@@ -1,1 +1,2 @@",
    " {",
    '+  "lockfileVersion": 3',
    "diff --git a/tests/test_svc.py b/tests/test_svc.py",
    "--- a/tests/test_svc.py",
    "+++ b/tests/test_svc.py",
    "@@ -1,1 +1,3 @@",
    " from app.svc import avg",
    "+def test_avg():",
    "+    assert avg([2, 4]) == 3",
    "diff --git a/Dockerfile b/Dockerfile",
    "--- a/Dockerfile",
    "+++ b/Dockerfile",
    "@@ -1,1 +1,2 @@",
    " FROM python:3.12",
    "+COPY . .",
    "",
])


def _files():
    return parse_diff(DIFF, [])


def _by_path():
    return {f.path: f for f in _files()}


def _f(file="app/svc.py", line=1, sev="medium", cat="bug", title="t", code="", source="llm", role="SE"):
    return Finding(file=file, line=line, severity=sev, category=cat, title=title, explanation="e",
                   existing_code=code, source=source, role=role)


# ---------- ĐX1: ghim bằng existing_code ----------

def test_resolve_anchor_ignores_wrong_line_number():
    svc = _by_path()["app/svc.py"]
    assert resolve_anchor(svc, "return sum(xs) / len(xs)", hint_line=40) == 6
    assert resolve_anchor(svc, "    if not xs:\n        return 0") == 4            # nhiều dòng liên tiếp
    assert resolve_anchor(svc, "+    return sum(xs) / len(xs)") == 6               # LLM chép cả dấu '+'
    assert resolve_anchor(svc, "sum(xs) / len(xs)") == 6                           # chép thiếu đầu dòng
    assert resolve_anchor(svc, "return sum(xs)/len(xs)   ") is None                # viết lại code -> không ghim
    assert resolve_anchor(svc, "") is None


def test_anchor_finding_records_how_it_was_anchored():
    svc = _by_path()["app/svc.py"]
    by_code = _f(line=99, code="return sum(xs) / len(xs)")
    assert anchor_finding(by_code, svc) == "code" and by_code.line == 6
    by_line = _f(line=4)
    assert anchor_finding(by_line, svc) == "line" and by_line.line == 4
    snapped = _f(line=11)
    assert anchor_finding(snapped, svc) == "snap" and snapped.line == 9
    lost = _f(line=500)
    assert anchor_finding(lost, svc) == "none"


def test_parse_output_drops_only_the_broken_finding():
    out = parse_output({"summary": "ok", "findings": [
        {"file": "a.py", "line": 0, "severity": "high", "category": "bug", "title": "t", "explanation": "e"},
        {"file": "a.py", "line": 3, "severity": "nghiêm trọng", "category": "bug", "title": "t", "explanation": "e"},
        {"file": "a.py", "line": "7", "severity": "low", "category": "style", "title": "t", "explanation": "e",
         "existing_code": None},
        "không phải object",
    ]})
    assert [f.line for f in out.findings] == [1, 7]      # line=0 được đưa về 1; severity sai bị bỏ riêng nó


# ---------- ĐX5: chọn file, chặn secret ----------

def test_secret_and_generated_paths():
    assert is_secret_path(".env") and is_secret_path("deploy/.env.production") and is_secret_path("keys/id_rsa")
    assert is_secret_path("secrets/app.json") and is_secret_path("certs/server.pem")
    assert not is_secret_path(".env.example") and not is_secret_path("app/environment.py")
    assert is_generated_path("package-lock.json") and is_generated_path("web/node_modules/x/index.js")
    assert is_generated_path("static/app.min.js") and not is_generated_path("requirements.txt")


def test_select_for_llm_reports_reasons():
    kept, skipped = select_for_llm(_files())
    assert {f.path for f in kept} == {"app/svc.py", "tests/test_svc.py", "Dockerfile"}
    assert {(s.path, s.reason) for s in skipped} == {(".env", "secret"), ("package-lock.json", "generated")}
    assert "không gửi lên LLM" in skipped[0].describe()


def test_each_role_gets_only_its_files():
    kept, _ = select_for_llm(_files())
    assert [f.path for f in files_for_role("SE", kept)] == ["app/svc.py"]
    assert [f.path for f in files_for_role("QA", kept)] == ["app/svc.py", "tests/test_svc.py"]
    assert [f.path for f in files_for_role("DevOps", kept)] == ["Dockerfile"]


def test_router_sends_test_only_change_to_qa():
    only_tests = [f for f in _files() if f.path == "tests/test_svc.py"]
    assert select_roles(only_tests, ["SE", "QA", "DevOps"]) == ["QA"]
    assert select_roles(_files(), ["SE", "QA", "DevOps"]) == ["SE", "QA", "DevOps"]


# ---------- ĐX4: chia lô, không cắt im lặng ----------

def test_build_batches_splits_and_records_truncation():
    items = [{"path": f"f{i}.py", "patch": "+x\n" * 100, "context": "c" * 100} for i in range(5)]
    batches, truncated = build_batches(items, max_prompt_chars=900, max_file_chars=5000)
    assert sum(len(b) for b in batches) == 5 and len(batches) > 1 and not truncated   # không file nào bị rơi

    big = [{"path": "big.py", "patch": "+line\n" * 5000, "context": "ctx\n" * 5000}]
    batches, truncated = build_batches(big, max_prompt_chars=60000, max_file_chars=3000)
    assert [(t.path, t.reason) for t in truncated] == [("big.py", "truncated")]
    assert TRUNCATION_MARK in batches[0][0]["patch"]
    assert len(batches[0][0]["patch"]) + len(batches[0][0]["context"]) <= 3000 + 2 * len(TRUNCATION_MARK) + 2


def test_redaction_hides_flagged_lines(tmp_path):
    svc = _by_path()["app/svc.py"]
    assert SECRET in render_patch(svc)
    red = render_patch(svc, {8})
    assert SECRET not in red and REDACTED in red and "return sum(xs)" in red
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "svc.py").write_text("\n".join(svc.new_lines.get(i, "") for i in range(1, 10)), encoding="utf-8")
    ctx = file_context(str(tmp_path), "app/svc.py", {8}, redact={8})
    assert SECRET not in ctx and REDACTED in ctx
    assert file_context(str(tmp_path), "../outside.py", set()) == ""            # không đọc ra ngoài repo


# ---------- ĐX3: checklist theo loại file ----------

def test_rules_resolve_by_file_type():
    assert rule_files_for("app/svc.py") == ["python.md"]
    assert rule_files_for("tests/test_svc.py") == ["python.md", "tests.md"]
    assert rule_files_for("Dockerfile") == ["dockerfile.md"]
    assert rule_files_for("web/src/App.tsx") == ["js_ts.md"]
    assert rule_files_for(".github/workflows/ci.yml") == ["ci.md"]
    assert rule_files_for("README.md") == ["default.md"]
    text = resolve_rules(["app/svc.py", "tests/test_svc.py", "Dockerfile"])
    assert "### Python" in text and "### File test" in text and "### Dockerfile" in text
    assert text.count("### Python") == 1 and "Không báo" in text and "CHÍNH XÁC" in text


# ---------- ĐX2: kiểm tra sự thật ----------

class _Judge:
    """Backend giả: trả về đúng danh sách 'remove' được cài sẵn."""
    def __init__(self, remove=None, fail=False):
        self.remove, self.fail, self.prompts = remove or [], fail, []

    def run_json(self, role, prompt, system=None):
        self.prompts.append((system, prompt))
        if self.fail:
            raise RuntimeError("LLM quá tải")
        return {"remove": self.remove}


def _ctx():
    svc = _by_path()["app/svc.py"]
    return [{"path": "app/svc.py", "patch": svc.patch_text, "context": "    4|     if not xs:\n    5|         return 0"}]


def test_fact_check_keeps_everything_by_default():
    out = AgentOutput(summary="s", findings=[_f(title="a"), _f(title="b")])
    kept, removed = fact_check(_Judge(), "SE", out, _ctx(), _by_path())
    assert [f.title for f in kept.findings] == ["a", "b"] and removed == []
    kept, removed = fact_check(_Judge(fail=True), "SE", out, _ctx(), _by_path())     # kiểm chứng hỏng -> giữ hết
    assert len(kept.findings) == 2 and removed == []


def test_fact_check_requires_evidence_that_exists_in_the_diff():
    wrong = _f(title="không kiểm tra danh sách rỗng", sev="medium", code="return sum(xs) / len(xs)")
    out = AgentOutput(findings=[wrong])
    # model bịa một dòng "bằng chứng" không có trong diff -> KHÔNG được loại
    kept, removed = fact_check(_Judge([{"index": 0, "ground": "B", "evidence": "if len(xs) == 0: raise ValueError"}]),
                               "SE", out, _ctx(), _by_path())
    assert len(kept.findings) == 1 and removed == []
    # bằng chứng là dòng có thật trong diff -> được loại, kèm lý do
    kept, removed = fact_check(_Judge([{"index": 0, "ground": "B", "evidence": "+    if not xs:", "reason": "đã kiểm tra"}]),
                               "SE", out, _ctx(), _by_path())
    assert kept.findings == [] and removed[0]["ground"] == "B" and "có thật" in removed[0]["check"]


def test_fact_check_ground_a_is_verified_by_code():
    present = _f(title="code có thật", code="return sum(xs) / len(xs)")
    absent = _f(title="code bịa", code="conn = sqlite3.connect(DB_PATH)")
    no_code = _f(title="không có existing_code")
    out = AgentOutput(findings=[present, absent, no_code])
    judge = _Judge([{"index": i, "ground": "A", "evidence": ""} for i in range(3)])
    kept, removed = fact_check(judge, "SE", out, _ctx(), _by_path())
    assert [f.title for f in kept.findings] == ["code có thật", "không có existing_code"]
    assert [r["title"] for r in removed] == ["code bịa"]


def test_fact_check_never_removes_protected_findings():
    sqli = _f(title="SQL injection", sev="critical", cat="security", code="không có trong diff")
    assert is_protected(sqli) and not is_protected(_f(sev="medium", cat="security")) and not is_protected(_f(sev="high", cat="test"))
    kept, removed = fact_check(_Judge([{"index": 0, "ground": "A", "evidence": ""}]), "SE",
                               AgentOutput(findings=[sqli]), _ctx(), _by_path())
    assert len(kept.findings) == 1 and removed == []


# ---------- ĐX6: dấu vân tay ----------

def test_fingerprint_survives_line_shift_and_rewording():
    a = _f(line=6, title="Chia cho 0 khi danh sách rỗng", code="return sum(xs) / len(xs)")
    b = _f(line=31, title="ZeroDivisionError nếu xs rỗng", code="    return sum(xs) / len(xs)  ")
    assert postprocess.fingerprint(a) == postprocess.fingerprint(b)
    assert postprocess.fingerprint(a) != postprocess.fingerprint(_f(line=6, cat="performance", code="return sum(xs) / len(xs)"))
    s1 = _f(title="B602", source="bandit", code="subprocess.run(cmd, shell=True)")
    s2 = _f(title="B604", source="bandit", code="subprocess.run(cmd, shell=True)")
    assert postprocess.fingerprint(s1) != postprocess.fingerprint(s2)       # hai luật khác nhau trên cùng dòng
    new, repeated = postprocess.split_new([a, s1], {postprocess.fingerprint(b)})
    assert new == [s1] and repeated == [a] and a.fingerprint


def test_posted_comments_roundtrip():
    from app import memory
    memory.init_db()
    assert memory.get_posted("o/r", 7) == set()
    memory.save_posted("o/r", 7, "sha1", ["aa", "bb", "aa"])
    memory.save_posted("o/r", 8, "sha1", ["cc"])
    assert memory.get_posted("o/r", 7) == {"aa", "bb"}


# ---------- Đường thật: run_llm_review ----------

class _Reviewer:
    """Backend giả ghi lại mọi prompt nó nhận, để kiểm tra thứ pipeline THỰC SỰ gửi cho LLM."""
    def __init__(self):
        self.prompts = []

    def run(self, role, prompt):
        self.prompts.append((role, prompt))
        findings = []
        if role == "SE":
            findings = [
                _f(file="b/app/svc.py", line=77, title="chia cho 0", code="return sum(xs) / len(xs)"),
                _f(file="app/khac.py", line=3, title="file không được giao"),
            ]
        return AgentOutput(summary=f"{role} xong", findings=findings)

    def run_json(self, role, prompt, system=None):
        self.prompts.append((role + ":factcheck", prompt))
        return {"remove": []}


def _run(tmp_path, **kw):
    (tmp_path / "app").mkdir(exist_ok=True)
    svc = _by_path()["app/svc.py"]
    (tmp_path / "app" / "svc.py").write_text("\n".join(svc.new_lines.get(i, "") for i in range(1, 10)), encoding="utf-8")
    backend = _Reviewer()
    leak = _f(file="app/svc.py", line=8, sev="critical", cat="security", title="Secret leaked: aws", source="gitleaks", role="static:gitleaks")
    res = run_llm_review(backend, ["SE", "QA", "DevOps"], _files(), str(tmp_path),
                         {"pr_number": 1, "title": "t", "body": ""}, [leak], **kw)
    return backend, res


def test_pipeline_never_sends_secrets_to_the_llm(tmp_path):
    backend, res = _run(tmp_path)
    everything = "\n".join(p for _, p in backend.prompts)
    assert backend.prompts and SECRET not in everything          # dòng gitleaks đánh dấu đã bị che
    assert "super-secret-value" not in everything                # file .env không bao giờ được gửi
    assert "lockfileVersion" not in everything                   # lockfile không tốn token
    assert REDACTED in everything
    assert {(s.path, s.reason) for s in res.skipped} == {(".env", "secret"), ("package-lock.json", "generated")}


def test_pipeline_anchors_scopes_and_applies_rules(tmp_path):
    backend, res = _run(tmp_path)
    assert [(f.file, f.line, f.anchor, f.role) for f in res.findings] == [("app/svc.py", 6, "code", "SE")]
    assert res.stats["dropped_out_of_scope"] == 1 and res.removed[0]["ground"] == "scope"
    se_prompt = next(p for r, p in backend.prompts if r == "SE")
    assert "### Python" in se_prompt and "Dockerfile" not in se_prompt and "tests/test_svc.py" not in se_prompt
    devops_prompt = next(p for r, p in backend.prompts if r == "DevOps")
    assert "### Dockerfile" in devops_prompt and "app/svc.py" not in devops_prompt
    assert res.summaries == {"SE": "SE xong", "QA": "QA xong", "DevOps": "DevOps xong"}
    assert any(r == "SE:factcheck" for r, _ in backend.prompts)          # mặc định là factcheck
    backend_off, _ = _run(tmp_path, verify_mode="off")
    assert not any(r.endswith(":factcheck") for r, _ in backend_off.prompts)


def test_pipeline_batches_instead_of_dropping(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_prompt_chars", 250)
    backend, res = _run(tmp_path, verify_mode="off")
    qa_calls = [p for r, p in backend.prompts if r == "QA"]
    assert len(qa_calls) == 2                                    # 2 file của QA không vừa 1 lô -> 2 lần gọi
    assert any("## app/svc.py" in p for p in qa_calls) and any("## tests/test_svc.py" in p for p in qa_calls)


def test_report_states_what_was_not_reviewed():
    md = report.build_markdown({"pr_number": 1, "title": "t", "owner": "o", "repo": "r", "head_sha": "abcdef123"},
                               ["SE"], {"SE": "ok"}, [_f(line=6)], "model-x", None,
                               skipped=[Skipped(".env", "secret"), Skipped("big.py", "truncated", "giữ 30000/90000 ký tự")],
                               removed=[{"file": "a.py", "line": 1, "title": "x", "ground": "B", "check": "dòng bằng chứng có thật"}])
    assert "## Phạm vi review" in md and "`.env`" in md and "giữ 30000/90000" in md
    assert "bị loại sau khi kiểm chứng" in md and md.rstrip().endswith("_")
    body = report.build_pr_summary({"SE": "ok"}, [], [], None, skipped=[Skipped(".env", "secret")], already_posted=3)
    assert "3 nhận xét từ lần review trước" in body and "không được review đầy đủ" in body
