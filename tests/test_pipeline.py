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


def test_dedupe_and_policy():
    files = parse_diff(DIFF, [])
    a = Finding(file="app/pay.py", line=3, severity="high", category="security", title="sqli", explanation="x", confidence=0.95, source="semgrep", role="static:semgrep")
    b = Finding(file="app/pay.py", line=3, severity="critical", category="security", title="sqli llm", explanation="y", confidence=0.8, source="llm", role="SE")
    c = Finding(file="app/pay.py", line=2, severity="low", category="style", title="naming", explanation="z", confidence=0.9, source="llm", role="SE")
    d = Finding(file="Dockerfile", line=2, severity="medium", category="devops", title="copy all", explanation="w", confidence=0.4, source="llm", role="DevOps")
    kept = postprocess.dedupe([a, b, c, d])
    assert len(kept) == 3 and any(k.source == "semgrep" for k in kept)   # ưu tiên static tool
    inline, overflow = postprocess.apply_policy(kept, files, 0.6, "medium", 10)
    assert [f.title for f in inline] == ["sqli"]      # low bị lọc, confidence 0.4 bị lọc
    assert postprocess.conclusion(inline, block_on_critical=True) == "neutral"


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
