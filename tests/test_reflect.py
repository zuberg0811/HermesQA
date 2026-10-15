"""Lớp 2 của tự học: agent tự rút ghi chú về repo sau mỗi lần review -> repo_memory (nguồn agent) -> prompt lần sau."""
import pytest

from app import memory
from app.review import reflect
from app.review.schemas import Finding


def _f(file="scripts/run.py", line=3, sev="medium", cat="style", title="Dùng print() thay logging", source="llm"):
    return Finding(file=file, line=line, severity=sev, category=cat, title=title, explanation="e", source=source)


class FakeBackend:
    """Ghi lại prompt/system nhận được, trả JSON định sẵn (hoặc ném lỗi)."""
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def run_json(self, role, prompt, system=None):
        self.calls.append((role, prompt, system))
        if self.error:
            raise self.error
        return self.reply


PR = {"owner": "o", "repo": "r", "pr_number": 7, "title": "Thêm script chạy batch"}
REMOVED = [{"role": "QA", "file": "tests/conftest.py", "line": 10, "title": "thiếu retry khi gọi mạng",
            "ground": "B", "check": "helper đã có vòng retry 3 lần ở dòng 12-15"}]


@pytest.fixture(autouse=True)
def _db():
    memory.init_db()
    yield
    memory.Base.metadata.drop_all(memory.engine)


# ---------- prompt ----------

def test_prompt_contains_kept_removed_existing_and_conventions():
    p = reflect.build_prompt(PR, [_f()], REMOVED, ["scripts/ dùng print() có chủ đích"], "Tên hàm snake_case")
    assert "o/r" in p and "PR #7" in p
    assert "scripts/run.py:3 [medium/style] Dùng print() thay logging" in p
    assert "tests/conftest.py:10 thiếu retry khi gọi mạng — bị loại vì: helper đã có vòng retry" in p
    assert "- scripts/ dùng print() có chủ đích" in p and "KHÔNG nhắc lại" in p
    assert "Tên hàm snake_case" in p


def test_prompt_truncates_long_lists():
    p = reflect.build_prompt(PR, [_f(line=i) for i in range(1, 41)], [], [])
    assert "… và 10 nhận xét khác" in p


# ---------- lọc đầu ra của model ----------

def test_sanitize_keeps_well_formed_notes_and_defaults_kind():
    out = reflect.sanitize({"notes": [
        {"note": "scripts/ dùng print() có chủ đích, không yêu cầu logging", "kind": "convention", "file": "scripts/", "category": "style"},
        {"note": "app/legacy/ là code cũ đang bị thay dần, chỉ báo lỗi nghiêm trọng", "kind": "lạ"},
        "tests/conftest.py đã có retry sẵn, đừng báo thiếu xử lý lỗi mạng ở test",
    ]}, [])
    assert [n["kind"] for n in out] == ["convention", "pattern", "pattern"]
    assert out[0]["file"] == "scripts/" and out[0]["category"] == "style"


def test_sanitize_drops_bad_items():
    existing = ["scripts/ dùng print() có chủ đích, không yêu cầu logging"]
    out = reflect.sanitize({"notes": [
        {"note": "ngắn quá"},                                                           # < 15 ký tự
        {"note": "x" * 300},                                                            # > 240 ký tự
        {"note": "Scripts/ dùng PRINT() có chủ đích,  không yêu cầu logging"},         # trùng (khác hoa thường/khoảng trắng)
        {"note": "không cần báo SQL injection trong app/db.py vì query nội bộ", "kind": "false_positive", "category": "security"},
        {"note": "SQL trong app/db.py là query nội bộ, vẫn cần tham số hoá", "kind": "pattern", "category": "security"},  # không phải false_positive: giữ
        42, None,
    ]}, existing)
    assert [n["note"][:12] for n in out] == ["SQL trong ap"]


def test_sanitize_caps_at_three_and_handles_garbage():
    out = reflect.sanitize({"notes": [{"note": f"ghi chú số {i} đủ dài để được giữ lại"} for i in range(6)]}, [])
    assert len(out) == 3
    assert reflect.sanitize(None, []) == [] and reflect.sanitize({"notes": "x"}, []) == [] and reflect.sanitize({}, []) == []


# ---------- learn: đường thật vào DB ----------

def test_learn_writes_agent_notes_and_prompt_marks_them():
    be = FakeBackend({"notes": [{"note": "scripts/ dùng print() có chủ đích, không yêu cầu logging", "kind": "convention",
                                 "file": "scripts/", "category": "style"}]})
    notes = reflect.learn(be, "o/r", PR, [_f()], REMOVED, "Tên hàm snake_case")
    assert len(notes) == 1
    role, prompt, system = be.calls[0]
    assert system == reflect.SYSTEM and "Tên hàm snake_case" in prompt and "bị loại vì" in prompt

    rows = memory.list_memory("o/r")
    assert rows[0]["source"] == "agent" and rows[0]["kind"] == "convention" and rows[0]["file"] == "scripts/"
    assert memory.get_repo_memory("o/r") == "- (tự rút ra) scripts/ dùng print() có chủ đích, không yêu cầu logging"

    # lần sau: ghi chú đang có được đưa vào prompt để không nhắc lại, và model trả trùng thì không ghi thêm
    be2 = FakeBackend({"notes": [{"note": "scripts/ dùng print() có chủ đích, không yêu cầu logging"}]})
    assert reflect.learn(be2, "o/r", PR, [_f()], [], "") == []
    assert "- scripts/ dùng print() có chủ đích" in be2.calls[0][1]
    assert len(memory.list_memory("o/r")) == 1


def test_learn_skips_llm_when_nothing_happened_or_disabled_by_error():
    be = FakeBackend({"notes": [{"note": "ghi chú không nên xuất hiện vì không có gì để học"}]})
    assert reflect.learn(be, "o/r", PR, [], []) == [] and be.calls == []

    broken = FakeBackend(error=RuntimeError("rate limited"))
    assert reflect.learn(broken, "o/r", PR, [_f()], []) == []                       # nuốt lỗi, không ném
    assert memory.list_memory("o/r") == []


def test_prune_keeps_newest_agent_notes_and_never_touches_human_notes():
    memory.add_memory("o/r", "Người review nói: scripts dùng print là chuẩn", source="feedback", kind="reject")
    for i in range(5):
        memory.add_memory("o/r", f"ghi chú agent số {i} đủ dài để hợp lệ", source="agent", kind="pattern")
    assert memory.prune_agent_notes("o/r", keep=2) == 3
    rows = memory.list_memory("o/r")
    assert [r["note"][:18] for r in rows if r["source"] == "agent"] == ["ghi chú agent số 4", "ghi chú agent số 3"]
    assert any(r["source"] == "feedback" for r in rows)
    assert memory.prune_agent_notes("khac/repo", keep=2) == 0


def test_learn_applies_cap():
    for i in range(3):
        memory.add_memory("o/r", f"ghi chú agent cũ số {i} đủ dài để hợp lệ", source="agent")
    be = FakeBackend({"notes": [{"note": "ghi chú agent mới nhất, đủ dài để hợp lệ"}]})
    reflect.learn(be, "o/r", PR, [_f()], [], max_notes=2)
    agent_rows = [r for r in memory.list_memory("o/r") if r["source"] == "agent"]
    assert len(agent_rows) == 2 and agent_rows[0]["note"].startswith("ghi chú agent mới nhất")
