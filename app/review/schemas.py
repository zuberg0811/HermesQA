from typing import Literal
from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low"]
SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


class Finding(BaseModel):
    file: str
    line: int = Field(ge=1)
    severity: Severity
    category: str                      # security | bug | performance | test | style | devops | ...
    title: str
    explanation: str
    suggested_fix: str = ""
    confidence: float = Field(ge=0, le=1, default=0.7)
    role: str = ""                     # SE | QA | DevOps | static:<tool>
    source: str = "llm"                # llm | semgrep | bandit | gitleaks | hadolint | ruff
    # 1-3 dòng code LLM chép NGUYÊN VĂN từ diff. Dùng để ghim finding bằng so khớp chuỗi
    # thay vì tin số dòng LLM tự đếm (LLM đếm dòng kém; xem diff_utils.resolve_anchor).
    existing_code: str = ""
    anchor: str = ""                   # code | line | snap | none — finding được ghim bằng cách nào
    fingerprint: str = ""              # dấu vân tay để không đăng lại comment cũ (postprocess.fingerprint)


class AgentOutput(BaseModel):
    summary: str = ""
    findings: list[Finding] = []


class ReviewConfig(BaseModel):
    language: str = "auto"
    roles: list[str] = ["SE", "QA", "DevOps"]
    ignore: list[str] = []
    conventions: str = ""
    max_comments: int | None = None
    block_on_critical: bool | None = None
