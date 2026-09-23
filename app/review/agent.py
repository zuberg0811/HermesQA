"""Agent backend: nạp SKILL.md của từng vai làm system prompt, gọi LLM/Hermes, ép JSON.
- OpenAICompatBackend: chạy ngay với OpenRouter / OpenAI / vLLM local (Qwen-Coder...).
- HermesCLIBackend: gọi Hermes Agent qua CLI (chỉnh HERMES_CMD theo bản Hermes bạn cài).
"""
import json
import logging
import re
import shlex
import subprocess
from pathlib import Path
from typing import Protocol

from openai import OpenAI

from app.config import settings
from app.review.schemas import AgentOutput

log = logging.getLogger("hermesqa.agent")

OUTPUT_CONTRACT = """
Bạn PHẢI trả về DUY NHẤT một JSON object (không markdown, không giải thích ngoài JSON) theo schema:
{
  "summary": "1-3 câu tóm tắt đánh giá tổng thể",
  "findings": [
    {
      "file": "đường/dẫn/file.py",
      "line": 42,
      "severity": "critical|high|medium|low",
      "category": "security|bug|performance|test|style|devops|maintainability",
      "title": "ngắn gọn < 80 ký tự",
      "explanation": "vì sao đây là vấn đề, hậu quả cụ thể",
      "suggested_fix": "code hoặc hướng sửa cụ thể",
      "confidence": 0.0-1.0
    }
  ]
}
Chỉ báo dòng NẰM TRONG diff (dòng có số ở cột trái trong phần context). Không bịa dòng.
Nếu không có vấn đề đáng kể: findings = [].
"""


def load_skill(role: str) -> str:
    return (settings.skills_dir / role / "SKILL.md").read_text(encoding="utf-8")


def build_user_prompt(pr_meta: dict, files_ctx: list[dict], static_summary: str, config_conventions: str, repo_memory: str) -> str:
    parts = [f"# Pull Request #{pr_meta['pr_number']}: {pr_meta['title']}\n{pr_meta.get('body','')[:1500]}\n"]
    if config_conventions:
        parts.append(f"# Convention của repo (bắt buộc tuân theo)\n{config_conventions}\n")
    if repo_memory:
        parts.append(f"# Ghi nhớ từ các lần review trước của repo này\n{repo_memory}\n")
    if static_summary:
        parts.append(f"# Static analysis đã phát hiện (KHÔNG lặp lại các lỗi này)\n{static_summary}\n")
    parts.append("# Các file thay đổi\n")
    for f in files_ctx:
        parts.append(f"## {f['path']}\n### Diff\n```diff\n{f['patch']}\n```\n### Context (số dòng | nội dung)\n```\n{f['context']}\n```\n")
    parts.append(OUTPUT_CONTRACT)
    return "\n".join(parts)


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


class AgentBackend(Protocol):
    def run(self, role: str, user_prompt: str) -> AgentOutput: ...


class OpenAICompatBackend:
    def __init__(self):
        # max_retries: SDK tự backoff khi gặp 429/5xx (Gemini/OpenRouter hay trả 503 lúc quá tải)
        self.client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key or "none",
                             max_retries=5, timeout=180.0)

    def run(self, role: str, user_prompt: str) -> AgentOutput:
        system = load_skill(role)
        resp = self.client.chat.completions.create(
            model=settings.llm_model, temperature=0.1, max_tokens=4000,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user_prompt}],
        )
        raw = resp.choices[0].message.content or "{}"
        usage = getattr(resp, "usage", None)
        if usage:
            log.info("[%s] tokens in=%s out=%s", role, usage.prompt_tokens, usage.completion_tokens)
        return AgentOutput.model_validate(_extract_json(raw))


class HermesCLIBackend:
    """Gọi Hermes Agent với skill dir tương ứng. Prompt qua stdin, kỳ vọng JSON trên stdout."""
    def run(self, role: str, user_prompt: str) -> AgentOutput:
        skill_dir = str(settings.skills_dir / role)
        cmd = shlex.split(settings.hermes_cmd.format(skill_dir=skill_dir))
        proc = subprocess.run(cmd, input=user_prompt, text=True, capture_output=True, timeout=600)
        if proc.returncode != 0:
            log.error("hermes failed: %s", proc.stderr[:500])
            raise RuntimeError("Hermes CLI failed")
        return AgentOutput.model_validate(_extract_json(proc.stdout))


def get_backend() -> AgentBackend:
    return HermesCLIBackend() if settings.agent_backend == "hermes_cli" else OpenAICompatBackend()


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


def self_verify(backend: AgentBackend, role: str, output: AgentOutput, files_ctx: list[dict]) -> AgentOutput:
    """Bước tự kiểm chứng để giảm hallucination. Bỏ qua nếu không có finding."""
    if not output.findings:
        return output
    code = "\n".join(f"## {f['path']}\n```diff\n{f['patch']}\n```\n```\n{f['context']}\n```" for f in files_ctx)
    # OUTPUT_CONTRACT nối SAU khi format: nó chứa dấu {} của JSON schema nên .format() sẽ hiểu nhầm là placeholder
    prompt = VERIFY_PROMPT.format(
        findings=json.dumps([f.model_dump() for f in output.findings], ensure_ascii=False, indent=1),
        code=code,
    ) + OUTPUT_CONTRACT
    try:
        verified = backend.run(role, prompt)
        verified.summary = verified.summary or output.summary
        return verified
    except Exception as e:  # không để bước verify làm hỏng cả review
        log.warning("self-verify failed (%s), keeping original", e)
        return output
