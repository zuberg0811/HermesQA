"""Lớp 5 của tự học: 👎 lặp lại trên một loại nhận xét -> hạ confidence -> rơi khỏi comment inline.

Hai nửa: hàm thuần (calibrate.Profile / calibrate.calibrate) và đường dữ liệu thật
(feedback -> memory.feedback_profile -> calibrate -> postprocess.apply_policy)."""
import pytest

from app import feedback, memory
from app.review import calibrate, postprocess
from app.review.diff_utils import parse_diff
from app.review.schemas import Finding


def _f(file="scripts/run.py", line=3, sev="medium", cat="style", title="Dùng print() thay logging", code="print(x)",
       source="llm", role="SE", conf=0.8):
    return Finding(file=file, line=line, severity=sev, category=cat, title=title, explanation="e",
                   existing_code=code, source=source, role=role, confidence=conf)


def _rows(n_reject, n_accept=0, **kw):
    base = {"file": "scripts/run.py", "category": "style", "title": "Dùng print()", "source": "llm", **kw}
    return [{"kind": "reject", **base}] * n_reject + [{"kind": "accept", **base}] * n_accept


# ---------- khoá đếm ----------

def test_keys_llm_use_category_and_top_dir():
    assert calibrate.keys_for(_f()) == ["llm:style | scripts", "llm:style"]
    assert calibrate.keys_for(_f(file="main.py")) == ["llm:style | .", "llm:style"]
    assert calibrate.keys_for(_f(file="a\\b\\c.py")) == ["llm:style | a", "llm:style"]


def test_keys_static_use_stable_rule_id_not_message():
    f = _f(file="tests/test_x.py", source="bandit", title="B101: assert_used", cat="security", sev="low")
    assert calibrate.keys_for(f) == ["bandit:B101 | tests", "bandit:B101"]
    assert calibrate.rule_key(_f(source="ruff", title="E501")) == "ruff:E501"


# ---------- hàm thuần ----------

def test_two_rejects_lower_confidence_and_explain():
    f = _f()
    adj = calibrate.calibrate([f], calibrate.Profile(_rows(2)))
    assert f.confidence == 0.4 and len(adj) == 1
    assert adj[0]["key"] == "llm:style | scripts" and adj[0]["before"] == 0.8
    assert "👎 2/2" in f.explanation and "80% → 40%" in f.explanation


def test_one_reject_is_not_enough():
    f = _f()
    assert calibrate.calibrate([f], calibrate.Profile(_rows(1))) == [] and f.confidence == 0.8


def test_accepts_outweigh_rejects():
    f = _f()
    assert calibrate.calibrate([f], calibrate.Profile(_rows(2, n_accept=2))) == []     # 50% < 67%
    assert calibrate.calibrate([f], calibrate.Profile(_rows(4, n_accept=1)))           # 80% >= 67%
    assert f.confidence == 0.4


def test_specific_key_wins_over_general():
    # Cùng loại llm:style nhưng 👎 chỉ ở scripts/ -> file ở app/ chỉ khớp khoá chung, và khoá chung cũng đủ 👎
    f_scripts, f_app = _f(), _f(file="app/svc.py")
    prof = calibrate.Profile(_rows(2))
    calibrate.calibrate([f_scripts, f_app], prof)
    assert f_scripts.confidence == 0.4 and f_app.confidence == 0.4
    # Nếu app/ có 👍 riêng, khoá cụ thể của app/ (0 reject) được ưu tiên và KHÔNG hạ
    prof2 = calibrate.Profile(_rows(2) + _rows(0, n_accept=1, file="app/other.py"))
    f_app2 = _f(file="app/svc.py")
    assert calibrate.calibrate([f_app2], prof2) == [] and f_app2.confidence == 0.8


def test_protected_findings_are_never_lowered():
    sec = _f(cat="security", sev="high", title="SQL injection")
    bug = _f(cat="bug", sev="critical", title="chia cho 0")
    low_bug = _f(cat="bug", sev="medium", title="off-by-one")
    rows = _rows(5, category="security", title="SQL injection") + _rows(5, category="bug", title="x")
    calibrate.calibrate([sec, bug, low_bug], calibrate.Profile(rows))
    assert sec.confidence == 0.8 and bug.confidence == 0.8
    assert low_bug.confidence == 0.4                                                   # bug mức medium thì được hạ


def test_empty_profile_or_findings_is_noop():
    f = _f()
    assert calibrate.calibrate([], calibrate.Profile(_rows(3))) == []
    assert calibrate.calibrate([f], calibrate.Profile([])) == [] and f.confidence == 0.8
    assert calibrate.calibrate([f], calibrate.Profile([{"kind": "huh", "file": "x"}])) == []


# ---------- đường dữ liệu thật: feedback -> profile -> calibrate -> apply_policy ----------

DIFF = """diff --git a/scripts/run.py b/scripts/run.py
--- a/scripts/run.py
+++ b/scripts/run.py
@@ -1,3 +1,4 @@
 import sys
+print(sys.argv)
 x = 1
 y = 2
"""


@pytest.fixture(autouse=True)
def _db():
    memory.init_db()
    yield
    memory.Base.metadata.drop_all(memory.engine)


def test_repeated_thumbs_down_drops_same_kind_of_comment_next_pr():
    # PR #1 và #2: bot báo print() trong scripts/, người 👎 cả hai lần
    for pr, cid in ((1, 101), (2, 102)):
        f = _f(line=2); postprocess.to_github_comment(f)
        memory.save_posted("o/r", pr, "sha", [f])
        memory.set_comment_ids("o/r", pr, {f.fingerprint: cid})
        assert feedback.record(memory.find_posted_by_comment(cid), "reject", "reaction")

    prof_rows = memory.feedback_profile("o/r")
    assert len(prof_rows) == 2 and prof_rows[0]["category"] == "style" and prof_rows[0]["file"] == "scripts/run.py"

    # PR #3: cùng loại nhận xét -> hạ 0.8 -> 0.4 -> dưới min_confidence 0.6 -> không còn comment inline
    files = parse_diff(DIFF, [])
    again = _f(line=2)
    other = _f(file="scripts/run.py", line=2, cat="performance", title="vòng lặp O(n^2)")   # loại khác: giữ nguyên
    adj = calibrate.calibrate([again, other], calibrate.Profile(prof_rows))
    assert [a["title"] for a in adj] == ["Dùng print() thay logging"]
    inline, overflow = postprocess.apply_policy([again, other], files, 0.6, "medium", 15)
    assert [f.title for f in inline] == ["vòng lặp O(n^2)"] and overflow == []


def test_feedback_profile_skips_feedback_without_posted_record():
    memory.save_feedback("o/r", 1, "deadbeefdeadbeef", 999, "reject", "reply")
    assert memory.feedback_profile("o/r") == []
    assert memory.feedback_profile("khac/repo") == []
