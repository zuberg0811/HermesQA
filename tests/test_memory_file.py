"""Memory từ FILE cho CLI/eval (A/B memory bật/tắt không cần DB): đọc file, xếp hạng y hệt worker, nối ghi chú reflect."""
import json

import pytest

from app import memory
from app.review import learn_rules


@pytest.fixture(autouse=True)
def _db():
    memory.init_db()
    yield
    memory.Base.metadata.drop_all(memory.engine)


ROWS = [  # thứ tự ghi vào file: cũ -> mới
    {"note": "cũ nhất, về file khác", "source": "agent", "file": "docs/x.md"},
    {"note": "chung: repo dùng pytest", "source": "agent"},
    {"note": "scripts/ dùng print() có chủ đích", "source": "feedback", "kind": "reject", "file": "scripts/run.py", "category": "style"},
    {"note": "mới nhất, về Dockerfile", "source": "agent", "file": "Dockerfile", "category": "devops"},
]


def test_load_memory_file_json_newest_first_and_defaults(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps(ROWS + [{"note": "  "}, "rác"]), encoding="utf-8")
    rows = memory.load_memory_file(str(p))
    assert [r["note"][:7] for r in rows] == ["mới nhấ", "scripts", "chung: ", "cũ nhất"]     # mới nhất trước, bỏ ghi chú rỗng
    assert rows[2]["file"] == "" and rows[2]["category"] == "" and rows[2]["kind"] == ""         # trường thiếu -> rỗng
    assert memory.load_memory_file(str(tmp_path / "khong_co.json")) == []


def test_load_memory_file_markdown_lines_are_human_general_notes(tmp_path):
    p = tmp_path / "m.md"
    p.write_text("# ghi chú\n- scripts/ dùng print()\n\n• app/legacy chỉ báo lỗi nặng\n", encoding="utf-8")
    rows = memory.load_memory_file(str(p))
    assert [r["note"] for r in rows] == ["app/legacy chỉ báo lỗi nặng", "scripts/ dùng print()"]
    assert all(r["source"] == "feedback" and r["file"] == "" for r in rows)


def test_rank_memory_matches_db_selection(tmp_path):
    """Cùng dữ liệu, chọn từ file phải ra đúng thứ tự như select_memory trên DB."""
    for r in ROWS:
        memory.add_memory("o/r", r["note"], source=r["source"], kind=r.get("kind", ""), file=r.get("file", ""),
                          category=r.get("category", ""))
    p = tmp_path / "m.json"
    p.write_text(json.dumps(ROWS), encoding="utf-8")
    from_file = [r["note"] for r in memory.rank_memory(memory.load_memory_file(str(p)), ["scripts/run.py"], "SE")]
    from_db = [r["note"] for r in memory.select_memory("o/r", ["scripts/run.py"], "SE")]
    assert from_file == from_db
    assert from_file[0] == "scripts/ dùng print() có chủ đích" and from_file[-1] == "cũ nhất, về file khác"
    txt = memory.memory_from_rows(memory.load_memory_file(str(p)), limit=2)(["scripts/run.py"], "SE")
    assert txt == "- scripts/ dùng print() có chủ đích\n- (tự rút ra) chung: repo dùng pytest"


def test_append_memory_file_creates_and_appends(tmp_path):
    p = tmp_path / "sub" / "m.json"
    n = memory.append_memory_file(str(p), [{"note": "ghi chú 1", "kind": "pattern", "file": "a/", "category": "bug"}])
    assert n == 1
    memory.append_memory_file(str(p), [{"note": "ghi chú 2"}])
    data = json.loads(p.read_text(encoding="utf-8"))
    assert [d["note"] for d in data] == ["ghi chú 1", "ghi chú 2"]
    assert data[0] == {"note": "ghi chú 1", "source": "agent", "kind": "pattern", "file": "a/", "category": "bug"}
    assert memory.load_memory_file(str(p))[0]["note"] == "ghi chú 2"                           # đọc lại: mới nhất trước


def test_collect_evidence_reads_external_evidence_file(tmp_path):
    p = tmp_path / "ev.json"
    p.write_text(json.dumps([
        {"repo": "x/y", "file": "pkg/a.py", "category": "style", "title": "Thiếu docstring", "reason": "giám khảo: không phải lỗi"},
        {"repo": "x/y", "file": "Dockerfile", "category": "devops", "title": "COPY . .", "reason": "đã có .dockerignore"},
        {"repo": "x/y", "file": "", "title": "bỏ qua vì không có file"},
    ]), encoding="utf-8")
    ev = learn_rules.collect_evidence([], str(p))
    assert len(ev["python.md"]) == 1 and "Thiếu docstring" in ev["python.md"][0] and "không phải lỗi" in ev["python.md"][0]
    assert len(ev["dockerfile.md"]) == 1
    assert sum(len(v) for v in ev.values()) == 2
