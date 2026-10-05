"""Chuẩn hoá output của semgrep / gitleaks / hadolint / ruff về Finding."""
import json
import os
from app.review.schemas import Finding

SEMGREP_SEV = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}
HADOLINT_SEV = {"error": "high", "warning": "medium", "info": "low", "style": "low"}
BANDIT_SEV = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}
BANDIT_CONF = {"HIGH": 0.9, "MEDIUM": 0.7, "LOW": 0.5}


# Ruff tron ca loi dung-sai lan loi hinh thuc trong cung mot dinh dang. Gan cung
# category="style" cho tat ca khien F821 (dung bien chua dinh nghia -> crash) bi
# xep ngang voi "thua mot dau cach", va lam buoc dedupe khong gop duoc voi LLM
# (LLM goi day la "bug").
_RUFF_BUG = {
    "F821": "high",    # bien chua dinh nghia -> NameError luc chay
    "F632": "high",    # dung "is" de so sanh literal
    "F811": "medium",  # dinh nghia de len chinh no
    "E711": "medium",  # so sanh None bang ==
    "E712": "medium",  # so sanh True/False bang ==
    "E713": "medium", "E714": "medium",
    "E722": "medium",  # bare except
    "B006": "medium",  # mutable default argument
    "B008": "medium",
}


def _ruff_meta(code: str) -> tuple[str, str]:
    """(category, severity) theo ma luat ruff."""
    sev = _RUFF_BUG.get(code)
    return ("bug", sev) if sev else ("style", "low")


STATIC_TOOLS = ("semgrep", "bandit", "gitleaks", "hadolint", "ruff")

# Luat static tool duoc coi la NHIEU khi bao trong file test. Hien chi co mot:
# bandit B101 "assert_used" — assert trong test la cach viet chuan cua pytest,
# chinh tai lieu bandit khuyen bo B101 cho test code. Tren bo eval, B101 chiem
# 11/26 FP cua cau hinh static (khong case nao la loi that).
# Luat o day phai la best practice duoc tai lieu cong cu ghi nhan, KHONG duoc
# them luat chi vi no lam diem eval dep hon.
_NOISE_IN_TESTS: dict[str, set[str]] = {"bandit": {"B101"}}


def is_test_path(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    base = parts[-1]
    return (any(d in ("test", "tests") for d in parts[:-1])
            or base.startswith("test_") or base.endswith("_test.py"))


def is_noise(f: Finding) -> bool:
    """Finding thuoc luat nhieu-trong-test (xem _NOISE_IN_TESTS)."""
    rules = _NOISE_IN_TESTS.get(f.source)
    if not rules or not is_test_path(f.file):
        return False
    rule_id = f.title.split(":", 1)[0].strip()
    return rule_id in rules


def tool_status(out_dir: str, findings: list[Finding]) -> dict[str, dict]:
    """Trang thai tung cong cu: chay duoc / that bai / khong co gi de quet.

    Bug "cong cu chay ma khong bao gi" da xay ra 4 lan trong du an nay (semgrep,
    bandit, self-verify, ruff). Mot lan chay hong truoc day trong Y HET mot lan
    chay sach. Ham nay lam cho hai truong hop do phan biet duoc.
    """
    failed = set()
    fpath = os.path.join(out_dir, "failed_tools.txt")
    if os.path.exists(fpath):
        with open(fpath, encoding="utf-8", errors="replace") as f:
            failed = {ln.strip() for ln in f if ln.strip()}
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.source] = counts.get(f.source, 0) + 1
    out = {}
    for t in STATIC_TOOLS:
        if t in failed:
            state = "failed"
        elif not os.path.exists(os.path.join(out_dir, f"{t}.json")):
            state = "missing"
        else:
            state = "ok"
        out[t] = {"state": state, "findings": counts.get(t, 0)}
    return out


def _load(path: str, default):
    try:
        with open(path) as f:
            txt = f.read().strip()
            return json.loads(txt) if txt else default
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _rel(p: str) -> str:
    return p.replace("/src/", "", 1).lstrip("/")


def collect(out_dir: str) -> list[Finding]:
    findings: list[Finding] = []

    for r in _load(os.path.join(out_dir, "semgrep.json"), {}).get("results", []):
        ex = r.get("extra", {})
        findings.append(Finding(
            file=_rel(r["path"]), line=r["start"]["line"],
            severity=SEMGREP_SEV.get(ex.get("severity", "WARNING"), "medium"),
            category="security" if "security" in str(ex.get("metadata", {})).lower() else "bug",
            title=r["check_id"].split(".")[-1], explanation=ex.get("message", ""),
            suggested_fix=ex.get("fix", "") or "", confidence=0.95, role="static:semgrep", source="semgrep"))

    for r in _load(os.path.join(out_dir, "bandit.json"), {}).get("results", []):
        findings.append(Finding(
            file=_rel(r.get("filename", "")), line=r.get("line_number", 1) or 1,
            severity=BANDIT_SEV.get(r.get("issue_severity", "LOW"), "low"), category="security",
            title=f"{r.get('test_id')}: {r.get('test_name')}", explanation=r.get("issue_text", ""),
            confidence=BANDIT_CONF.get(r.get("issue_confidence", "MEDIUM"), 0.7),
            role="static:bandit", source="bandit"))

    for r in _load(os.path.join(out_dir, "gitleaks.json"), []):
        findings.append(Finding(
            file=_rel(r.get("File", "")), line=r.get("StartLine", 1) or 1, severity="critical", category="security",
            title=f"Secret leaked: {r.get('RuleID')}", explanation="Phát hiện chuỗi giống secret/API key được hard-code trong mã nguồn.",
            suggested_fix="Xoá secret khỏi code, xoay (rotate) key, chuyển sang biến môi trường / secret manager.",
            confidence=0.9, role="static:gitleaks", source="gitleaks"))

    # hadolint được append nhiều mảng JSON liên tiếp -> parse từng dòng
    try:
        with open(os.path.join(out_dir, "hadolint.json")) as f:
            for chunk in f.read().split("]["):
                chunk = chunk if chunk.startswith("[") else "[" + chunk
                chunk = chunk if chunk.endswith("]") else chunk + "]"
                for r in json.loads(chunk):
                    findings.append(Finding(
                        file=_rel(r["file"]), line=r["line"], severity=HADOLINT_SEV.get(r["level"], "low"),
                        category="devops", title=r["code"], explanation=r["message"], confidence=0.9,
                        role="static:hadolint", source="hadolint"))
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    for r in _load(os.path.join(out_dir, "ruff.json"), []):
        cat, sev = _ruff_meta(r.get("code") or "")
        findings.append(Finding(
            file=_rel(r["filename"]), line=r["location"]["row"], severity=sev, category=cat,
            title=r["code"], explanation=r["message"], suggested_fix=(r.get("fix") or {}).get("message", "") or "",
            confidence=0.9, role="static:ruff", source="ruff"))

    return findings
