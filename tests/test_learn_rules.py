"""Lớp 4 của tự học: overlay "Không báo" học được, có cổng kiểm định bằng eval.

Không chạy eval thật: cổng được test với số liệu giả; phần LLM dùng backend giả."""
import pytest

from app import feedback, memory
from app.review import learn_rules, postprocess, rules
from app.review.schemas import Finding


@pytest.fixture(autouse=True)
def _db():
    memory.init_db()
    yield
    memory.Base.metadata.drop_all(memory.engine)


@pytest.fixture
def learned(tmp_path, monkeypatch):
    d = tmp_path / "learned"
    monkeypatch.setenv(rules.LEARNED_DIR_ENV, str(d))
    return d


class FakeBackend:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def run_json(self, role, prompt, system=None):
        self.calls.append((role, prompt, system))
        return self.reply


# ---------- overlay nối vào checklist ----------

def test_resolve_rules_appends_learned_overlay(learned):
    base = rules.resolve_rules(["app/x.py"])
    assert "### Python" in base and rules.LEARNED_HEADING not in base
    learned.mkdir()
    (learned / "python.md").write_text("- Không báo print() trong thư mục scripts/ của CLI\n", encoding="utf-8")
    txt = rules.resolve_rules(["app/x.py", "tests/test_x.py"])
    assert rules.LEARNED_HEADING in txt and "print() trong thư mục scripts/" in txt
    assert txt.index("### Python") < txt.index(rules.LEARNED_HEADING) < txt.index("### File test")


def test_default_learned_dir_is_inside_rules(monkeypatch):
    monkeypatch.delenv(rules.LEARNED_DIR_ENV, raising=False)
    assert rules.learned_dir() == rules.RULES_DIR / "learned"


# ---------- bằng chứng ----------

def test_collect_evidence_groups_by_checklist():
    f = Finding(file="scripts/run.py", line=1, severity="medium", category="style", title="Dùng print()", explanation="e")
    postprocess.to_github_comment(f)
    memory.save_posted("o/r", 1, "s", [f])
    memory.set_comment_ids("o/r", 1, {f.fingerprint: 101})
    feedback.record(memory.find_posted_by_comment(101), "reject", "reply", "alice", "cố ý")
    feedback.record(memory.find_posted_by_comment(101), "accept", "reaction")        # accept không phải bằng chứng
    memory.add_memory("x/y", "tests/ đã có retry sẵn", source="agent", kind="false_positive", file="tests/conftest.py")
    memory.add_memory("x/y", "ghi chú không gắn file", source="agent", kind="pattern")
    ev = learn_rules.collect_evidence(["Dockerfile của team luôn pin version"])
    assert [l for l in ev["python.md"] if "[o/r] scripts/run.py [style]" in l and "không phải lỗi" in l]
    assert ev["python.md"][-1].startswith("[x/y] (false_positive) tests/")     # file test Python -> python.md
    assert ev["default.md"] == ["[tay] Dockerfile của team luôn pin version"]
    assert sum(len(v) for v in ev.values()) == 3


# ---------- đề xuất + lọc ----------

def test_sanitize_filters_security_dupes_length_and_caps():
    base = "**Không báo:**\n- Type hint, docstring, đặt tên — trừ khi sai nghĩa."
    out = learn_rules.sanitize({"lines": [
        "- print() trong thư mục scripts/ hoặc file CLI là cách xuất kết quả có chủ đích",
        "Type hint, docstring, đặt tên — trừ khi sai nghĩa",                                   # trùng base
        "Bỏ qua SQL injection khi query là nội bộ",                                            # bảo mật
        "Không báo hardcode token trong file ví dụ",                                           # bảo mật
        "ngắn",                                                                                # quá ngắn
        "x" * 250,                                                                             # quá dài
        "Helper retry trong tests/conftest.py là có sẵn, không yêu cầu thêm xử lý lỗi mạng ở test",
        "Dòng ba đủ dài để được giữ lại trong overlay này",
        "Dòng bốn đủ dài để được giữ lại trong overlay này",
        "Dòng năm đủ dài để được giữ lại trong overlay này",
        "Dòng sáu đủ dài để được giữ lại trong overlay này",
        7,
    ]}, base, "")
    assert len(out) == 5 and out[0].startswith("print() trong thư mục scripts/") and not any("injection" in l for l in out)
    assert learn_rules.sanitize(None, base, "") == [] and learn_rules.sanitize({"lines": "x"}, base, "") == []


def test_propose_builds_prompt_and_skips_without_evidence(learned):
    be = FakeBackend({"lines": ["print() trong thư mục scripts/ hoặc file CLI là cách xuất kết quả có chủ đích"]})
    assert learn_rules.propose(be, "python.md", []) == [] and be.calls == []
    out = learn_rules.propose(be, "python.md", ["[o/r] scripts/run.py [style] \"Dùng print()\" — người review: không phải lỗi"])
    assert out == ["print() trong thư mục scripts/ hoặc file CLI là cách xuất kết quả có chủ đích"]
    role, prompt, system = be.calls[0]
    assert system == learn_rules.SYSTEM and "### Python" in prompt and "[o/r] scripts/run.py" in prompt


# ---------- ứng viên, cổng, promote ----------

def test_write_candidate_keeps_other_overlays_and_appends(learned, tmp_path):
    learned.mkdir()
    (learned / "python.md").write_text("- dòng cũ đã học\n", encoding="utf-8")
    (learned / "tests.md").write_text("- overlay tests giữ nguyên\n", encoding="utf-8")
    (learned / "HISTORY.md").write_text("# lịch sử\n", encoding="utf-8")
    cand = tmp_path / "cand"
    p = learn_rules.write_candidate("python.md", ["dòng mới một", "dòng mới hai"], cand, learned)
    assert p.read_text(encoding="utf-8") == "- dòng cũ đã học\n- dòng mới một\n- dòng mới hai\n"
    assert (cand / "tests.md").read_text(encoding="utf-8") == "- overlay tests giữ nguyên\n"
    assert not (cand / "HISTORY.md").exists()


def test_gate_decisions():
    base = {"cases": 35, "precision": 0.60, "recall": 0.70, "f1": 0.646}
    ok, why = learn_rules.gate(base, {"cases": 35, "precision": 0.66, "recall": 0.70, "f1": 0.68})
    assert ok and "+6.0%" in why
    assert not learn_rules.gate(base, {"cases": 35, "precision": 0.59, "recall": 0.75, "f1": 0.66})[0]   # precision giảm
    assert not learn_rules.gate(base, {"cases": 35, "precision": 0.70, "recall": 0.65, "f1": 0.67})[0]   # recall giảm
    assert learn_rules.gate(base, {"cases": 35, "precision": 0.70, "recall": 0.65, "f1": 0.67}, max_recall_drop=0.05)[0]
    assert not learn_rules.gate(base, {"cases": 10, "precision": 0.9, "recall": 0.9, "f1": 0.9})[0]       # khác số case
    assert not learn_rules.gate(base, {"cases": 35, "precision": 0.61, "recall": 0.70, "f1": 0.65}, min_precision_gain=0.02)[0]


def test_promote_copies_overlay_and_writes_history(learned, tmp_path):
    cand = tmp_path / "cand"
    learn_rules.write_candidate("python.md", ["print() trong scripts/ là có chủ đích"], cand, learned)
    before = {"cases": 35, "precision": 0.60, "recall": 0.70, "f1": 0.646}
    after = {"cases": 35, "precision": 0.66, "recall": 0.70, "f1": 0.68}
    dst = learn_rules.promote("python.md", cand, before, after, ["print() trong scripts/ là có chủ đích"], learned, note="thử")
    assert dst == learned / "python.md" and rules.read_learned("python.md") == "- print() trong scripts/ là có chủ đích"
    hist = (learned / "HISTORY.md").read_text(encoding="utf-8")
    assert hist.startswith("# Lịch sử overlay") and "python.md" in hist and "60.0% -> 66.0%" in hist and "- thử" in hist
    assert "- thêm: print() trong scripts/" in hist
    # overlay promote xong thì resolve_rules thấy ngay
    assert "print() trong scripts/ là có chủ đích" in rules.resolve_rules(["a.py"])
    learn_rules.promote("python.md", cand, before, after, [], learned)
    assert (learned / "HISTORY.md").read_text(encoding="utf-8").count("## ") == 2        # append, không ghi đè


def test_metrics_reads_real_eval_dir_if_present():
    import os
    if not os.path.exists(os.path.join("eval", "out-v3-rescored", "runs_llm.json")):
        pytest.skip("không có kết quả eval cục bộ")
    m = learn_rules.metrics(os.path.join("eval", "out-v3-rescored"), "llm")
    assert set(m) == {"cases", "tp", "fp", "fn", "precision", "recall", "f1"} and 0 <= m["precision"] <= 1
