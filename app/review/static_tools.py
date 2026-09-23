"""Chuẩn hoá output của semgrep / gitleaks / hadolint / ruff về Finding."""
import json
import os
from app.review.schemas import Finding

SEMGREP_SEV = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}
HADOLINT_SEV = {"error": "high", "warning": "medium", "info": "low", "style": "low"}
BANDIT_SEV = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}
BANDIT_CONF = {"HIGH": 0.9, "MEDIUM": 0.7, "LOW": 0.5}


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
        findings.append(Finding(
            file=_rel(r["filename"]), line=r["location"]["row"], severity="low", category="style",
            title=r["code"], explanation=r["message"], suggested_fix=(r.get("fix") or {}).get("message", "") or "",
            confidence=0.9, role="static:ruff", source="ruff"))

    return findings
