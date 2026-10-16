"""Lớp 3 của tự học: memory chọn theo lô file + vai, thay vì 20 ghi chú mới nhất."""
import pytest

from app import memory
from app.review.diff_utils import parse_diff
from app.review.pipeline import run_llm_review
from app.review.schemas import AgentOutput


@pytest.fixture(autouse=True)
def _db():
    memory.init_db()
    yield
    memory.Base.metadata.drop_all(memory.engine)


def _seed():
    memory.add_memory("o/r", "cũ nhất, về file khác", source="agent", file="docs/x.md")
    memory.add_memory("o/r", "chung: repo dùng pytest, fixture ở tests/conftest.py", source="agent")
    memory.add_memory("o/r", "scripts/ dùng print() có chủ đích", source="feedback", kind="reject", file="scripts/run.py", category="style")
    memory.add_memory("o/r", "app/legacy/ là code cũ, chỉ báo lỗi nghiêm trọng", source="agent", file="app/legacy", category="bug")
    memory.add_memory("o/r", "tests/ đã có retry sẵn", source="agent", file="tests/conftest.py", category="test")
    memory.add_memory("o/r", "mới nhất, về Dockerfile", source="agent", file="Dockerfile", category="devops")


def test_path_score_levels():
    assert memory._path_score("scripts/run.py", ["scripts/run.py"]) == 3
    assert memory._path_score("scripts/other.py", ["scripts/run.py"]) == 2      # cùng thư mục
    assert memory._path_score("app/legacy", ["app/legacy/old.py"]) == 2         # tiền tố thư mục
    assert memory._path_score("", ["x.py"]) == 1                                 # ghi chú chung
    assert memory._path_score("Dockerfile", ["scripts/run.py"]) == 0


def test_select_prefers_matching_file_then_general_then_others():
    _seed()
    rows = memory.select_memory("o/r", ["scripts/run.py"], role="SE")
    notes = [r["note"] for r in rows]
    assert notes[0] == "scripts/ dùng print() có chủ đích"                      # trùng file
    assert notes[1].startswith("chung:")                                         # ghi chú chung
    assert notes[-1] == "cũ nhất, về file khác"                                  # file khác, cũ nhất: cuối
    # limit cắt từ dưới lên: với limit 2 chỉ còn ghi chú liên quan + chung
    assert [r["note"][:7] for r in memory.select_memory("o/r", ["scripts/run.py"], limit=2)] == ["scripts", "chung: "]


def test_role_category_breaks_ties_and_human_notes_first():
    _seed()
    # Lô ở tests/: ghi chú tests/ lên đầu; với vai QA, category test được ưu tiên hơn devops khi cùng hạng
    rows = memory.select_memory("o/r", ["tests/test_a.py"], role="QA")
    assert rows[0]["note"] == "tests/ đã có retry sẵn"
    others = [r["note"] for r in rows if r["file"] not in ("tests/conftest.py", "")]
    assert others.index("mới nhất, về Dockerfile") < others.index("cũ nhất, về file khác")   # cùng hạng 0: mới hơn trước
    # Ghi chú do người dạy xếp trước ghi chú agent khi cùng hạng đường dẫn
    memory.add_memory("o/r", "agent nói về tests/x.py", source="agent", file="tests/x.py")
    rows = memory.select_memory("o/r", ["tests/x.py"], role="QA")
    assert rows[0]["note"] == "agent nói về tests/x.py"                          # trùng file (3) thắng cùng thư mục (2)
    assert rows[1]["note"] == "tests/ đã có retry sẵn"


def test_memory_text_marks_agent_notes_and_empty_repo():
    _seed()
    txt = memory.memory_text(memory.select_memory("o/r", ["scripts/run.py"], limit=2))
    assert txt.splitlines()[0] == "- scripts/ dùng print() có chủ đích"
    assert txt.splitlines()[1].startswith("- (tự rút ra) chung:")
    assert memory.select_memory("khac/repo", ["a.py"]) == [] and memory.memory_text([]) == ""


# ---------- pipeline gọi callable theo từng lô ----------

DIFF = """diff --git a/scripts/run.py b/scripts/run.py
--- a/scripts/run.py
+++ b/scripts/run.py
@@ -1,2 +1,3 @@
 import sys
+print(sys.argv)
 x = 1
diff --git a/tests/test_a.py b/tests/test_a.py
--- a/tests/test_a.py
+++ b/tests/test_a.py
@@ -1,2 +1,3 @@
 import pytest
+def test_x(): assert 1
 y = 2
"""


class _Backend:
    def __init__(self):
        self.prompts = []

    def run(self, role, prompt):
        self.prompts.append((role, prompt))
        return AgentOutput(summary="ok", findings=[])

    def run_json(self, role, prompt, system=None):
        return {"summary": "ok", "findings": []}


def test_pipeline_gives_each_batch_its_own_memory(tmp_path):
    _seed()
    files = parse_diff(DIFF, [])
    for f in files:
        p = tmp_path / f.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("import sys\nprint(sys.argv)\nx = 1\n" if "scripts" in f.path else "import pytest\ndef test_x(): assert 1\ny = 2\n")
    be = _Backend()
    run_llm_review(be, ["SE", "QA"], files, str(tmp_path), {"pr_number": 1, "title": "t", "body": ""}, [],
                   "", memory.memory_for_batch("o/r", limit=2), verify_mode="off")
    by_role = {role: prompt for role, prompt in be.prompts}
    assert "scripts/ dùng print() có chủ đích" in by_role["SE"] and "tests/ đã có retry sẵn" not in by_role["SE"]
    assert "tests/ đã có retry sẵn" in by_role["QA"]                              # QA nhận cả hai file -> có ghi chú tests/
    assert "mới nhất, về Dockerfile" not in by_role["SE"] + by_role["QA"]          # file không liên quan bị đẩy ra ngoài limit
    # chuỗi tĩnh vẫn chạy như cũ
    be2 = _Backend()
    run_llm_review(be2, ["SE"], files, str(tmp_path), {"pr_number": 1, "title": "t", "body": ""}, [],
                   "", "- ghi chú tĩnh", verify_mode="off")
    assert "- ghi chú tĩnh" in be2.prompts[0][1]
