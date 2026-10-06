"""Agent backend: nạp SKILL.md của từng vai làm system prompt, gọi LLM/Hermes, ép JSON, kiểm chứng.
- OpenAICompatBackend: chạy ngay với OpenRouter / OpenAI / Gemini / vLLM local (Qwen-Coder...).
- AnthropicBackend: SDK chính thức của Anthropic.
- HermesCLIBackend: gọi Hermes Agent qua CLI (chỉnh HERMES_CMD theo bản Hermes bạn cài).
"""
import json
import logging
import re
import shlex
import subprocess
import time
from typing import Protocol

from openai import OpenAI, RateLimitError
from pydantic import ValidationError

from app.config import settings
from app.review.diff_utils import ChangedFile, resolve_anchor
from app.review.schemas import AgentOutput, Finding, SEVERITY_RANK

log = logging.getLogger("hermesqa.agent")

OUTPUT_CONTRACT = """
Bạn PHẢI trả về DUY NHẤT một JSON object (không markdown, không giải thích ngoài JSON) theo schema:
{
  "summary": "1-3 câu tóm tắt đánh giá tổng thể",
  "findings": [
    {
      "file": "đường/dẫn/file.py",
      "line": 42,
      "existing_code": "1-3 dòng code CHÉP NGUYÊN VĂN từ diff tại chỗ có vấn đề",
      "severity": "critical|high|medium|low",
      "category": "security|bug|performance|test|style|devops|maintainability",
      "title": "ngắn gọn < 80 ký tự",
      "explanation": "vì sao đây là vấn đề, hậu quả cụ thể",
      "suggested_fix": "code hoặc hướng sửa cụ thể",
      "confidence": 0.0-1.0
    }
  ]
}
`existing_code` dùng để ghim nhận xét vào đúng dòng: hệ thống tìm đoạn này trong diff bằng so khớp chuỗi.
Chép từ các dòng '+' của diff, KHÔNG kèm dấu '+', KHÔNG sửa hay rút gọn một ký tự nào. Nếu bạn viết lại
hoặc tóm tắt code, nhận xét sẽ không được ghim đúng chỗ.
Chỉ báo dòng NẰM TRONG diff (dòng có số ở cột trái trong phần context). Không bịa dòng.
Nếu không có vấn đề đáng kể: findings = [].
"""


def load_skill(role: str) -> str:
    return (settings.skills_dir / role / "SKILL.md").read_text(encoding="utf-8")


def build_user_prompt(pr_meta: dict, files_ctx: list[dict], static_summary: str, config_conventions: str,
                      repo_memory: str, rules_text: str = "") -> str:
    parts = [f"# Pull Request #{pr_meta['pr_number']}: {pr_meta['title']}\n{pr_meta.get('body','')[:1500]}\n"]
    if config_conventions:
        parts.append(f"# Convention của repo (bắt buộc tuân theo)\n{config_conventions}\n")
    if repo_memory:
        parts.append(f"# Ghi nhớ từ các lần review trước của repo này\n{repo_memory}\n")
    if rules_text:
        parts.append(f"# Checklist theo loại file (bắt buộc, kể cả mục \"Không báo\")\n{rules_text}\n")
    if static_summary:
        parts.append(f"# Static analysis đã phát hiện (KHÔNG lặp lại các lỗi này)\n{static_summary}\n")
    parts.append("# Các file thay đổi (bạn chỉ được nhận xét về các file này)\n")
    for f in files_ctx:
        parts.append(f"## {f['path']}\n### Diff\n```diff\n{f['patch']}\n```\n### Context (số dòng | nội dung)\n```\n{f['context']}\n```\n")
    parts.append(OUTPUT_CONTRACT)
    return "\n".join(parts)


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    # strict=False: model hay để xuống dòng thật trong chuỗi (vd existing_code nhiều dòng) — hợp lệ với ta
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0), strict=False)


_RETRY_DELAY = re.compile(r"retry(?:Delay'?:\s*'?|\s+in\s+)(\d+(?:\.\d+)?)\s*s", re.I)
RATE_LIMIT_MAX_WAIT = 120.0   # giây; hạn mức NGÀY (retry sau hàng giờ) thì không đợi, báo lỗi ngay


def rate_limit_wait(err_text: str) -> float | None:
    """Thời gian cần đợi theo thông điệp 429 của Gemini, hoặc None nếu không nên đợi."""
    m = _RETRY_DELAY.search(err_text)
    if not m:
        return None
    delay = float(m.group(1))
    return delay + 1.0 if delay <= RATE_LIMIT_MAX_WAIT else None


def parse_output(data: dict) -> AgentOutput:
    """dict -> AgentOutput, bỏ TỪNG finding hỏng thay vì bỏ cả lô.

    Trước đây `AgentOutput.model_validate` làm một finding sai kiểu (vd line = 0) kéo đổ toàn bộ
    kết quả của vai. Số dòng không hợp lệ được đưa về 1 và để bước ghim bằng existing_code sửa lại.
    """
    if not isinstance(data, dict):
        raise ValueError("LLM không trả về JSON object")
    findings = []
    for raw in data.get("findings") or []:
        if not isinstance(raw, dict):
            continue
        d = dict(raw)
        line = d.get("line")
        if isinstance(line, str) and line.strip().isdigit():
            line = int(line.strip())
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            line = 1
        d["line"] = line
        for k in ("existing_code", "suggested_fix", "explanation", "title"):
            if d.get(k) is None:
                d[k] = ""
        try:
            findings.append(Finding.model_validate(d))
        except ValidationError as e:
            log.warning("bỏ 1 finding không hợp lệ (%s): %s", e.errors()[0].get("loc"), str(raw)[:160])
    summary = data.get("summary")
    return AgentOutput(summary=summary if isinstance(summary, str) else "", findings=findings)


class AgentBackend(Protocol):
    def run(self, role: str, user_prompt: str) -> AgentOutput: ...
    def run_json(self, role: str, user_prompt: str, system: str | None = None) -> dict: ...


class _JsonBackend:
    """Phần chung: backend chỉ cần cài `complete()` trả về văn bản thô."""

    def complete(self, role: str, user_prompt: str, system: str | None = None) -> str:  # pragma: no cover
        raise NotImplementedError

    def run_json(self, role: str, user_prompt: str, system: str | None = None) -> dict:
        # JSON hỏng (thiếu dấu phẩy, ngoặc kép chưa escape) là lỗi ngẫu nhiên của model ~1/15 PR thật:
        # gọi lại đúng một lần trước khi coi là vai thất bại.
        try:
            return _extract_json(self.complete(role, user_prompt, system))
        except (json.JSONDecodeError, ValueError) as e:
            log.warning("[%s] JSON hỏng (%s), gọi lại một lần", role, e)
            return _extract_json(self.complete(role, user_prompt, system))

    def run(self, role: str, user_prompt: str) -> AgentOutput:
        return parse_output(self.run_json(role, user_prompt))


class OpenAICompatBackend(_JsonBackend):
    def __init__(self):
        # max_retries: SDK tự backoff khi gặp 429/5xx (Gemini/OpenRouter hay trả 503 lúc quá tải)
        self.client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key or "none",
                             max_retries=5, timeout=180.0)

    def complete(self, role: str, user_prompt: str, system: str | None = None) -> str:
        messages = [{"role": "system", "content": system or load_skill(role)},
                    {"role": "user", "content": user_prompt}]
        # Gemini free tier có hạn mức THEO PHÚT (vd 15 req/phút) với retryDelay ~1 phút — dài hơn backoff
        # của SDK, nên tự đợi đúng thời gian được báo rồi thử lại; hạn mức NGÀY thì ném lỗi ngay.
        for attempt in range(3):
            try:
                resp = self.client.chat.completions.create(model=settings.llm_model, temperature=0.1,
                                                           max_tokens=4000, messages=messages)
                break
            except RateLimitError as e:
                wait = rate_limit_wait(str(e))
                if wait is None or attempt == 2:
                    raise
                log.warning("[%s] 429 theo phút, đợi %.0fs rồi thử lại", role, wait)
                time.sleep(wait)
        usage = getattr(resp, "usage", None)
        if usage:
            log.info("[%s] tokens in=%s out=%s", role, usage.prompt_tokens, usage.completion_tokens)
        return resp.choices[0].message.content or "{}"


class AnthropicBackend(_JsonBackend):
    """Gọi Claude qua SDK chính thức của Anthropic (không qua lớp OpenAI-compat).

    Cùng contract prompt/JSON với OpenAICompatBackend để so sánh model được công bằng.
    - system prompt (SKILL.md của vai) ổn định theo vai -> đánh dấu cache_control.
    - fallbacks="default": nếu bộ lọc an toàn từ chối (review code có SQLi, command
      injection... là nội dung hợp lệ nhưng có thể bị phân loại "cyber"), API tự chạy
      lại trên model dự phòng trong cùng request.
    - stop_reason == "refusal" sau cả chuỗi -> raise để caller đánh dấu vai lỗi,
      không để trả về findings rỗng giả vờ "không có vấn đề".
    """
    def __init__(self, client=None):
        if client is None:
            import anthropic
            kwargs = {"max_retries": 5, "timeout": 300.0}
            if settings.anthropic_api_key:
                kwargs["api_key"] = settings.anthropic_api_key
            client = anthropic.Anthropic(**kwargs)
        self.client = client

    def complete(self, role: str, user_prompt: str, system: str | None = None) -> str:
        resp = self.client.beta.messages.create(
            model=settings.anthropic_model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": settings.anthropic_effort},
            system=[{"type": "text", "text": system or load_skill(role), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_prompt}],
        )
        if resp.stop_reason == "refusal":
            detail = getattr(resp, "stop_details", None)
            raise RuntimeError(f"Claude từ chối request (category={getattr(detail, 'category', None)})")
        if resp.stop_reason == "max_tokens":
            log.warning("[%s] output bị cắt vì max_tokens", role)
        usage = getattr(resp, "usage", None)
        if usage:
            cached = getattr(usage, "cache_read_input_tokens", 0) or 0
            log.info("[%s] tokens in=%s out=%s (cache_read=%s, model=%s)", role,
                     usage.input_tokens + cached, usage.output_tokens, cached, resp.model)
        return "".join(b.text for b in resp.content if b.type == "text") or "{}"


class HermesCLIBackend(_JsonBackend):
    """Gọi Hermes Agent với skill dir tương ứng. Prompt qua stdin, kỳ vọng JSON trên stdout."""
    def complete(self, role: str, user_prompt: str, system: str | None = None) -> str:
        skill_dir = str(settings.skills_dir / role)
        cmd = shlex.split(settings.hermes_cmd.format(skill_dir=skill_dir))
        if system:  # Hermes lấy system từ skill dir; chỉ thị riêng (vd fact-check) phải đi kèm trong prompt
            user_prompt = f"{system}\n\n{user_prompt}"
        proc = subprocess.run(cmd, input=user_prompt, text=True, capture_output=True, timeout=600)
        if proc.returncode != 0:
            log.error("hermes failed: %s", proc.stderr[:500])
            raise RuntimeError("Hermes CLI failed")
        return proc.stdout


def get_backend() -> AgentBackend:
    if settings.agent_backend == "hermes_cli":
        return HermesCLIBackend()
    if settings.agent_backend == "anthropic":
        return AnthropicBackend()
    return OpenAICompatBackend()


# ---------------------------------------------------------------------------------------------
# Kiểm chứng kiểu CŨ (giữ lại làm đối chứng cho thực nghiệm; không còn là mặc định)
# ---------------------------------------------------------------------------------------------

VERIFY_PROMPT = """Bạn là reviewer cấp cao kiểm chứng lại các phát hiện của một AI khác.
Với MỖI finding dưới đây, đối chiếu với diff/context. Giữ lại chỉ những finding:
- đúng file, đúng dòng (hoặc lệch tối đa 3 dòng — hãy sửa số dòng),
- thật sự là vấn đề, không phải phong cách cá nhân,
- không trùng nội dung với finding khác.
Trả về JSON cùng schema, findings đã lọc và có confidence cập nhật.

# Findings cần kiểm chứng
{findings}

# Code
{code}
"""


def _code_block(files_ctx: list[dict]) -> str:
    return "\n".join(f"## {f['path']}\n```diff\n{f['patch']}\n```\n```\n{f['context']}\n```" for f in files_ctx)


def self_verify(backend: AgentBackend, role: str, output: AgentOutput, files_ctx: list[dict]) -> AgentOutput:
    """Bước tự kiểm chứng kiểu cũ: nhờ LLM "chỉ giữ finding thật sự là vấn đề".

    Ablation đo được: bật/tắt chênh 0,7 điểm F1 (bằng dao động giữa hai lần chạy y hệt) mà tốn
    gấp đôi token. Lý do thiết kế: câu hỏi này mời model xoá bất cứ thứ gì nó không chắc, và
    model sinh lại toàn bộ danh sách nên có thể sửa cả những finding đúng. Thay bằng fact_check().
    """
    if not output.findings:
        return output
    # OUTPUT_CONTRACT nối SAU khi format: nó chứa dấu {} của JSON schema nên .format() sẽ hiểu nhầm là placeholder
    prompt = VERIFY_PROMPT.format(
        findings=json.dumps([f.model_dump() for f in output.findings], ensure_ascii=False, indent=1),
        code=_code_block(files_ctx),
    ) + OUTPUT_CONTRACT
    try:
        verified = backend.run(role, prompt)
        verified.summary = verified.summary or output.summary
        return verified
    except Exception as e:  # không để bước verify làm hỏng cả review
        log.warning("self-verify failed (%s), keeping original", e)
        return output


# ---------------------------------------------------------------------------------------------
# Kiểm chứng kiểu MỚI: kiểm tra sự thật, mặc định GIỮ, mọi lần loại phải có bằng chứng kiểm được
# ---------------------------------------------------------------------------------------------

FACTCHECK_SYSTEM = (
    "Bạn là người kiểm tra sự thật cho các nhận xét review code. Bạn không viết nhận xét mới, "
    "không đánh giá nhận xét có hữu ích hay không. Bạn chỉ tìm nhận xét bị diff chứng minh là sai."
)

FACTCHECK_PROMPT = """Dưới đây là các nhận xét review do một AI khác viết, và code mà chúng nói tới.
Nhiệm vụ rất hẹp: chỉ ra những nhận xét mà code dưới đây CHỨNG MINH là sai sự thật.

Hai loại sai lầm KHÔNG ngang nhau:
- Giữ một nhận xét sai: người đọc mất vài giây.
- Bỏ một nhận xét đúng: một lỗi thật lọt ra ngoài và không ai biết nó đã bị bỏ.
Vì vậy khi bằng chứng chưa đủ để CHỨNG MINH, hãy giữ. "Đáng ngờ", "tôi không kiểm chứng được",
"giá trị thấp", "tôi sẽ không nêu điều này" đều có nghĩa là GIỮ.

Chỉ có hai căn cứ để loại một nhận xét:
A — Đoạn code mà nhận xét mô tả KHÔNG tồn tại trong diff/context của file nó nêu.
B — Một dòng cụ thể trong diff/context PHỦ ĐỊNH TRỰC TIẾP điều nhận xét khẳng định
    (ví dụ: nhận xét nói "không kiểm tra danh sách rỗng" trong khi ngay đó có dòng `if not xs: return`).
    Phải đọc được ngay từ dòng đó, không qua nhiều bước suy luận.

KHÔNG phải căn cứ để loại:
- Bạn không đồng ý với đề xuất, hoặc thấy code hiện tại chấp nhận được.
- Nhận xét nói về style, đặt tên, khả năng bảo trì — miễn là điều nó nói là đúng.
- Nhận xét suy luận về hành vi lúc chạy hoặc về code ở file bạn không thấy.
- Nhận xét trích hơi lệch dòng nhưng ý chính đúng.

# Nhận xét cần kiểm tra (mỗi nhận xét có "index")
<<FINDINGS>>

# Code
<<CODE>>

Trả về DUY NHẤT một JSON object, không markdown:
{"remove": [{"index": 0, "ground": "A hoặc B", "evidence": "với B: CHÉP NGUYÊN VĂN một dòng trong diff/context phủ định nhận xét; với A: chuỗi rỗng", "reason": "một câu"}]}
Mặc định là {"remove": []}. Với đa số lần review, đó là câu trả lời đúng.
"""

_CTX_PREFIX = re.compile(r"^\s*\d+\|\s?")


def is_protected(f: Finding) -> bool:
    """Nhóm không bao giờ bị bước kiểm chứng loại: lỗi bảo mật / bug mức high trở lên.
    Đây là nơi loại nhầm đắt nhất, và cũng là nơi sự tự tin của model kém tin cậy nhất."""
    return f.category in ("security", "bug") and SEVERITY_RANK[f.severity] >= SEVERITY_RANK["high"]


def _norm(s: str) -> str:
    return " ".join(s.split())


def _code_lines(ctx: dict) -> list[str]:
    """Các dòng code (đã chuẩn hoá) của một file: từ patch (bỏ dấu +/-/khoảng trắng) và context (bỏ 'NN| ')."""
    out = []
    for row in ctx.get("patch", "").splitlines():
        if row.startswith(("+++", "---", "@@")):
            continue
        out.append(_norm(row[1:] if row[:1] in "+- " else row))
    for row in ctx.get("context", "").splitlines():
        out.append(_norm(_CTX_PREFIX.sub("", row)))
    return [l for l in out if l]


def _removal_proven(f: Finding, item: dict, files_ctx: list[dict], files_by_path: dict[str, ChangedFile]) -> tuple[bool, str]:
    """Phần mềm kiểm lại "bằng chứng" của model trước khi cho loại. Trả (được loại?, lý do)."""
    if is_protected(f):
        return False, "nhóm được bảo vệ"
    ground = str(item.get("ground", "")).strip().upper()[:1]
    if ground == "A":
        cf = files_by_path.get(f.file)
        if cf is None:
            return True, "file không nằm trong diff"
        if not f.existing_code.strip():
            return False, "nhận xét không có existing_code nên không chứng minh được là code vắng mặt"
        if resolve_anchor(cf, f.existing_code, f.line) is not None:
            return False, "existing_code CÓ trong diff"
        own = next((c for c in files_ctx if c["path"] == f.file), None)
        lines = _code_lines(own) if own else []
        probe = max((_norm(l) for l in f.existing_code.splitlines()), key=len, default="")
        if probe and any(probe in l for l in lines):
            return False, "existing_code CÓ trong context"
        return True, "existing_code không xuất hiện trong diff/context của file"
    if ground == "B":
        ev = _norm(str(item.get("evidence", "")))
        ev = _norm(_CTX_PREFIX.sub("", ev))
        if ev[:1] in "+-" and len(ev) > 1:
            ev = _norm(ev[1:])
        if len(ev) < 6:
            return False, "bằng chứng quá ngắn hoặc trống"
        # Căn cứ B nghĩa là "code đã xử lý ở CHỖ KHÁC". Nếu bằng chứng chính là dòng bị báo thì model đang
        # lý luận vòng ("nhận xét nói thiếu X, nhưng dòng 12 đã có X" — trong khi dòng 12 chính là chỗ sai).
        # Eval cho thấy flash-lite loại 4/5 TP theo kiểu này.
        own_lines = [_norm(l) for l in f.existing_code.splitlines()] if f.existing_code else []
        if any(ev == l or (ev in l) or (l and l in ev) for l in own_lines if len(l) >= 6):
            return False, "bằng chứng trùng chính dòng bị báo (không phải chỗ khác đã xử lý)"
        for c in files_ctx:
            if any(ev in l for l in _code_lines(c)):
                return True, "dòng bằng chứng có thật trong diff/context"
        return False, "dòng bằng chứng KHÔNG có trong diff/context (model bịa bằng chứng)"
    return False, "căn cứ không hợp lệ"


def fact_check(backend: AgentBackend, role: str, output: AgentOutput, files_ctx: list[dict],
               files_by_path: dict[str, ChangedFile]) -> tuple[AgentOutput, list[dict]]:
    """Kiểm tra sự thật: mặc định giữ mọi finding; chỉ loại khi model nêu căn cứ A/B VÀ phần mềm
    kiểm được căn cứ đó (xem _removal_proven). Trả (output đã lọc, danh sách bị loại kèm lý do).

    Khác bản cũ ở ba điểm: (1) model không sinh lại finding nên không thể làm hỏng cái đúng;
    (2) "bằng chứng" phải là một dòng có thật trong diff — model không thể loại bằng cảm giác;
    (3) nhóm security/bug mức high+ không bao giờ bị loại.
    """
    if not output.findings:
        return output, []
    listing = json.dumps(
        [{"index": i, "file": f.file, "line": f.line, "category": f.category, "severity": f.severity,
          "title": f.title, "explanation": f.explanation, "existing_code": f.existing_code}
         for i, f in enumerate(output.findings)], ensure_ascii=False, indent=1)
    prompt = FACTCHECK_PROMPT.replace("<<FINDINGS>>", listing).replace("<<CODE>>", _code_block(files_ctx))
    try:
        data = backend.run_json(role, prompt, system=FACTCHECK_SYSTEM)
    except Exception as e:  # kiểm chứng hỏng thì giữ nguyên, không làm hỏng cả review
        log.warning("fact-check failed (%s), keeping all findings", e)
        return output, []

    drop: dict[int, dict] = {}
    items = data.get("remove") if isinstance(data, dict) else None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < len(output.findings)) or idx in drop:
            continue
        f = output.findings[idx]
        ok, why = _removal_proven(f, item, files_ctx, files_by_path)
        if ok:
            drop[idx] = {"role": role, "file": f.file, "line": f.line, "title": f.title,
                         "ground": str(item.get("ground", "")).strip().upper()[:1],
                         "evidence": str(item.get("evidence", ""))[:200],
                         "reason": str(item.get("reason", ""))[:200], "check": why,
                         "finding": f.model_dump()}   # giữ nguyên finding để eval có thể chấm lại offline
        else:
            log.info("[%s] fact-check muốn loại '%s' nhưng bị từ chối: %s", role, f.title, why)
    kept = [f for i, f in enumerate(output.findings) if i not in drop]
    return AgentOutput(summary=output.summary, findings=kept), list(drop.values())


def verify(mode: str, backend: AgentBackend, role: str, output: AgentOutput, files_ctx: list[dict],
           files_by_path: dict[str, ChangedFile]) -> tuple[AgentOutput, list[dict]]:
    """mode: factcheck | legacy | off."""
    if mode == "off":
        return output, []
    if mode == "legacy":
        return self_verify(backend, role, output, files_ctx), []
    return fact_check(backend, role, output, files_ctx, files_by_path)
