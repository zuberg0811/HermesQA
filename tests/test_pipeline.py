"""Test không cần mạng/LLM: parse diff, router, postprocess."""
import os
os.environ.setdefault("GITHUB_APP_ID", "1")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "x")

from app.review.diff_utils import parse_diff, commentable_line
from app.review.router import select_roles
from app.review import postprocess
from app.review.agent import self_verify
from app.review.schemas import AgentOutput, Finding

DIFF = """diff --git a/app/pay.py b/app/pay.py
--- a/app/pay.py
+++ b/app/pay.py
@@ -1,1 +1,4 @@
 import os
+def create_payment(amount):
+    q = "SELECT * FROM users WHERE id = " + str(amount)
+    return q
diff --git a/Dockerfile b/Dockerfile
--- a/Dockerfile
+++ b/Dockerfile
@@ -1,1 +1,3 @@
 FROM python:latest
+COPY . .
+CMD python app.py
"""


def test_parse_and_router():
    files = parse_diff(DIFF, [])
    assert {f.path for f in files} == {"app/pay.py", "Dockerfile"}
    py = next(f for f in files if f.path == "app/pay.py")
    assert 2 in py.added_lines and 4 in py.added_lines
    assert select_roles(files, ["SE", "QA", "DevOps"]) == ["SE", "QA", "DevOps"]
    assert select_roles(files, ["SE"]) == ["SE"]


def test_commentable_line_snaps():
    files = parse_diff(DIFF, [])
    py = next(f for f in files if f.path == "app/pay.py")
    assert commentable_line(py, 3) == 3
    assert commentable_line(py, 6) == 4      # lệch 2 dòng -> snap
    assert commentable_line(py, 50) is None


def _f(file, line, sev, cat, title, source="llm", role="SE", conf=0.9):
    return Finding(file=file, line=line, severity=sev, category=cat, title=title, explanation=title + " expl",
                   confidence=conf, source=source, role=role)


def test_dedupe_and_policy():
    files = parse_diff(DIFF, [])
    a = _f("app/pay.py", 3, "high", "security", "sqli", source="semgrep", role="static:semgrep", conf=0.95)
    b = _f("app/pay.py", 3, "critical", "security", "sqli llm", conf=0.8)
    c = _f("app/pay.py", 2, "low", "style", "naming")
    d = _f("Dockerfile", 2, "medium", "devops", "copy all", role="DevOps", conf=0.4)
    kept = postprocess.dedupe([a, b, c, d])
    sqli = [k for k in kept if k.title == "sqli"]
    assert len(sqli) == 1 and sqli[0].source == "semgrep"   # cùng category: ưu tiên static tool
    assert sqli[0].severity == "critical"                     # nhưng severity lấy mức cao nhất của cụm
    assert "sqli llm" in sqli[0].explanation                  # finding bị gộp được đính kèm, không mất
    inline, overflow = postprocess.apply_policy(kept, files, 0.6, "medium", 10)
    assert [f.title for f in inline] == ["sqli"]      # low bị lọc, confidence 0.4 bị lọc
    assert postprocess.conclusion(inline, block_on_critical=True) == "failure"


def test_dedupe_static_vs_llm_merges_across_category():
    """ruff E711 gọi '== None' là bug, LLM gọi cùng lỗi đó là maintainability -> phải gộp làm một."""
    ruff = _f("app/api.py", 12, "medium", "bug", "E711", source="ruff", role="static:ruff")
    llm = _f("app/api.py", 12, "low", "maintainability", "So sánh None bằng ==", role="SE")
    kept = postprocess.dedupe([ruff, llm])
    assert len(kept) == 1 and kept[0].source == "ruff"


def test_dedupe_does_not_absorb_more_severe_llm_finding():
    """conclusion() chỉ nhìn finding chính -> finding critical không được giấu vào ghi chú của finding medium.
    semgrep medium (no-direct-write) cạnh nil-deref critical của LLM là hai lỗi khác nhau: giữ cả hai."""
    sg = _f("server/handler.go", 22, "medium", "security", "no-direct-write", source="semgrep", role="static:semgrep")
    llm = _f("server/handler.go", 22, "critical", "bug", "Nil pointer dereference", role="SE", conf=1.0)
    kept = postprocess.dedupe([sg, llm])
    assert {k.title for k in kept} == {"no-direct-write", "Nil pointer dereference"}
    assert postprocess.conclusion(kept, block_on_critical=True) == "failure"
    # nhưng nhận xét LLM nhẹ hơn hoặc bằng thì được hấp thụ vào finding static
    note = _f("server/handler.go", 21, "medium", "test", "Thiếu unit test", role="QA")
    kept = postprocess.dedupe([sg, note])
    assert len(kept) == 1 and kept[0].source == "semgrep" and "Thiếu unit test" in kept[0].explanation


def test_dedupe_keeps_distinct_llm_roles():
    """SE báo bug và QA báo thiếu test cho cùng hàm là hai nhận xét khác nhau, không được gộp."""
    se = _f("app/utils.py", 16, "high", "bug", "Thiếu xử lý lỗi", role="SE")
    qa = _f("app/utils.py", 15, "high", "test", "Thiếu unit test", role="QA")
    assert len(postprocess.dedupe([se, qa])) == 2


def test_bandit_assert_in_tests_is_noise():
    from app.review.static_tools import is_noise
    in_test = _f("tests/test_utils.py", 5, "low", "security", "B101: assert_used", source="bandit", role="static:bandit")
    in_src = _f("app/utils.py", 5, "low", "security", "B101: assert_used", source="bandit", role="static:bandit")
    other = _f("tests/test_utils.py", 5, "high", "security", "B602: subprocess_popen_with_shell_equals_true", source="bandit", role="static:bandit")
    assert is_noise(in_test) and not is_noise(in_src) and not is_noise(other)


def test_self_verify_builds_prompt_without_format_error():
    """Prompt verify nối OUTPUT_CONTRACT (chứa {} của JSON schema) nên không được đưa qua .format()."""
    captured = {}

    class FakeBackend:
        def run(self, role, prompt):
            captured["prompt"] = prompt
            return AgentOutput(summary="", findings=[])

    out = AgentOutput(summary="s", findings=[
        Finding(file="app/pay.py", line=3, severity="high", category="security",
                title="sqli", explanation="x", confidence=0.9, source="llm", role="SE")])
    verified = self_verify(FakeBackend(), "SE", out, [{"path": "app/pay.py", "patch": "p", "context": "c"}])

    assert "prompt" in captured, "backend không được gọi -> self_verify đã nuốt lỗi"
    assert '"summary"' in captured["prompt"]        # OUTPUT_CONTRACT có mặt nguyên vẹn
    assert verified.summary == "s"                  # summary cũ được giữ khi verify trả rỗng


def test_collect_applies_noise_filter(tmp_path):
    """is_noise() phải được gọi BÊN TRONG collect(): lần đầu bộ lọc có test riêng nhưng
    không được nối vào collect(), nên eval thật vẫn ra 11 FP B101 trong khi rescore thì không."""
    import json
    from app.review.static_tools import collect
    bandit = {"results": [
        {"filename": "/src/tests/test_utils.py", "line_number": 5, "issue_severity": "LOW",
         "issue_confidence": "HIGH", "test_id": "B101", "test_name": "assert_used", "issue_text": "assert"},
        {"filename": "/src/app/utils.py", "line_number": 9, "issue_severity": "HIGH",
         "issue_confidence": "HIGH", "test_id": "B602", "test_name": "subprocess_popen_with_shell_equals_true", "issue_text": "shell"},
    ]}
    (tmp_path / "bandit.json").write_text(json.dumps(bandit), encoding="utf-8")
    titles = [f.title for f in collect(str(tmp_path))]
    assert titles == ["B602: subprocess_popen_with_shell_equals_true"]


def test_anthropic_backend_parses_text_and_rejects_refusal():
    """Backend Anthropic: lấy text block -> JSON; stop_reason refusal phải raise chứ không trả findings rỗng."""
    from types import SimpleNamespace as NS
    from app.review.agent import AnthropicBackend

    class FakeMessages:
        def __init__(self, resp):
            self.resp, self.calls = resp, []

        def create(self, **kw):
            self.calls.append(kw)
            return self.resp

    def backend_with(resp):
        fm = FakeMessages(resp)
        client = NS(beta=NS(messages=fm))
        return AnthropicBackend(client=client), fm

    ok = NS(stop_reason="end_turn", model="claude-opus-5-5",
            content=[NS(type="thinking", thinking=""), NS(type="text", text='{"summary":"s","findings":[]}')],
            usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))
    be, fm = backend_with(ok)
    out = be.run("SE", "prompt")
    assert out.summary == "s" and fm.calls[0]["messages"][0]["content"] == "prompt"
    assert fm.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}

    refused = NS(stop_reason="refusal", model="claude-opus-5-5", content=[],
                 stop_details=NS(category="cyber"), usage=None)
    be, _ = backend_with(refused)
    import pytest
    with pytest.raises(RuntimeError):
        be.run("SE", "prompt")
