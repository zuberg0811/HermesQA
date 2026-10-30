"""Hai lỗi nhãn tìm thấy khi đo eval 15/10 (docs/EVAL_CLAUDECODE_2026-10-15.md, mục 4):

1. semgrep gắn "bug" cho luật Dockerfile -> cụm gộp (semgrep nặng hơn hadolint) mất nhãn "devops" -> mất TP.
2. Gộp finding LLM vào cụm static khác category thì category của LLM biến mất -> score không khớp ground truth.

Test đi qua đúng đường chạy thật: static_tools.collect() đọc semgrep.json như runner ghi ra, postprocess.dedupe()
như worker/cli gọi, và score.matched_indices() của eval/score.py chấm trên model_dump() như run_eval ghi file.
"""
import json
import sys
from pathlib import Path

from app.review import postprocess, static_tools
from app.review.schemas import Finding

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
import score  # noqa: E402  (eval/score.py)


def _semgrep_result(check_id, path, line, severity="WARNING", metadata=None):
    return {"check_id": check_id, "path": path, "start": {"line": line},
            "extra": {"severity": severity, "message": "m", "metadata": metadata or {}}}


def _collect(tmp_path, results):
    (tmp_path / "semgrep.json").write_text(json.dumps({"results": results}), encoding="utf-8")
    return {f.title: f for f in static_tools.collect(str(tmp_path))}


def test_semgrep_dockerfile_rules_are_devops(tmp_path):
    out = _collect(tmp_path, [
        # luật best-practice của semgrep-rules: metadata.technology = [dockerfile]
        _semgrep_result("dockerfile.best-practice.missing-assume-yes-switch.missing-assume-yes-switch", "src/Dockerfile", 3,
                        metadata={"category": "best-practice", "technology": ["dockerfile"]}),
        # luật security giữ nguyên security (ground truth Dockerfile chấp nhận cả devops lẫn security)
        _semgrep_result("dockerfile.security.missing-user.missing-user", "src/Dockerfile", 6, "ERROR",
                        metadata={"category": "security", "technology": ["dockerfile"], "cwe": ["CWE-250"]}),
        # không có metadata: vẫn nhận ra qua tiền tố check_id hoặc tên file
        _semgrep_result("dockerfile.best-practice.prefer-copy-over-add.prefer-copy-over-add", "src/Dockerfile", 5),
        _semgrep_result("custom.rule", "src/deploy/app.dockerfile", 2),
        _semgrep_result("yaml.github-actions.best-practice.x.x", "src/.github/workflows/ci.yml", 9,
                        metadata={"category": "best-practice", "technology": ["github-actions"]}),
        # luật Python không-security vẫn là bug như cũ
        _semgrep_result("python.lang.correctness.useless-eqeq.useless-eqeq", "src/app/x.py", 7,
                        metadata={"category": "correctness", "technology": ["python"]}),
    ])
    assert out["missing-assume-yes-switch"].category == "devops"
    assert out["missing-user"].category == "security"
    assert out["prefer-copy-over-add"].category == "devops"
    assert out["rule"].category == "devops"
    assert out["x"].category == "devops"
    assert out["useless-eqeq"].category == "bug"


def _f(file, line, sev, cat, title, source="llm", role="SE"):
    return Finding(file=file, line=line, severity=sev, category=cat, title=title, explanation="e",
                   confidence=0.9, source=source, role=role)


def test_dedupe_keeps_absorbed_categories():
    """Cụm giữ nhãn chính của finding chính, nhãn khác của finding bị gộp đi vào `categories`."""
    hadolint = _f("Dockerfile", 4, "medium", "devops", "DL3004", source="hadolint", role="static:hadolint")
    semgrep = _f("Dockerfile", 4, "high", "bug", "no-sudo", source="semgrep", role="static:semgrep")   # nhãn cũ (sai)
    llm = _f("Dockerfile", 4, "low", "security", "sudo trong image", role="DevOps")
    (kept,) = postprocess.dedupe([hadolint, semgrep, llm])
    assert kept.title == "no-sudo" and kept.category == "bug"
    assert kept.categories == ["devops", "security"]          # không trùng, không chứa nhãn chính
    # finding có categories phụ bị gộp tiếp thì nhãn phụ của nó cũng được mang theo
    other = _f("Dockerfile", 4, "medium", "bug", "khác", source="ruff", role="static:ruff")
    other.categories = ["performance"]
    (kept2,) = postprocess.dedupe([kept, other])
    assert kept2.categories == ["devops", "security", "performance"]
    # finding không bị gộp gì thì categories rỗng và model_dump vẫn chứa trường này (run_eval ghi ra file)
    alone = _f("app/x.py", 1, "low", "bug", "a")
    assert postprocess.dedupe([alone])[0].model_dump()["categories"] == []
    # JSON cũ không có trường này vẫn nạp được (eval/rescore chấm lại kết quả cũ)
    assert Finding.model_validate({"file": "a", "line": 1, "severity": "low", "category": "bug",
                                   "title": "t", "explanation": "e"}).categories == []


def test_score_matches_on_secondary_category():
    truths = [{"file": "Dockerfile", "line": 4, "categories": ["devops", "security"]}]
    primary_only = {"file": "Dockerfile", "line": 4, "category": "bug"}
    assert score.matched_indices([primary_only], truths) == {}                     # như trước: trượt
    with_secondary = {**primary_only, "categories": ["devops"]}
    assert score.matched_indices([with_secondary], truths) == {0: 0}               # nhãn phụ cứu lại TP
    # vẫn phải đúng file và trong dung sai dòng
    wrong_file = {**with_secondary, "file": "other"}
    assert score.matched_indices([wrong_file], truths) == {}
