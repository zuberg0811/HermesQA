import os
import re
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Profile LLM: LLM_<TÊN>_BASE_URL / LLM_<TÊN>_API_KEY / LLM_<TÊN>_MODEL (và tuỳ chọn LLM_<TÊN>_BACKEND)
PROFILE_KEY_RE = re.compile(r"^llm_(.+?)_(base_url|api_key|model|backend)$")


class Settings(BaseSettings):
    # extra="allow": các khoá LLM_<TÊN>_* trong .env không phải field cố định nhưng phải được giữ lại (xem llm_profiles)
    model_config = SettingsConfigDict(env_file=".env", extra="allow")

    # GitHub App
    github_app_id: str
    github_webhook_secret: str
    github_private_key_path: str = "/app/secrets/github-app.pem"

    # Agent
    agent_backend: str = "openai_compat"  # openai_compat | hermes_cli
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = "anthropic/claude-sonnet-4"
    # Nhiều nhà cung cấp OpenAI-compatible khai báo SONG SONG trong .env để so sánh model trong eval:
    #   LLM_GEMINI_BASE_URL / LLM_GEMINI_API_KEY / LLM_GEMINI_MODEL
    #   LLM_MUSE_BASE_URL   / LLM_MUSE_API_KEY   / LLM_MUSE_MODEL      (Muse Spark của Meta, api.meta.ai/v1)
    # LLM_PROFILE=muse (hoặc --profile muse ở cli/run_eval) đè bộ đó lên llm_base_url/llm_api_key/llm_model.
    # Để trống -> dùng LLM_BASE_URL/LLM_API_KEY/LLM_MODEL như cũ, không đổi hành vi hiện tại.
    llm_profile: str = ""
    hermes_cmd: str = "hermes run --skill {skill_dir} --json"
    # Backend "anthropic": SDK chính thức. Key để trống -> SDK tự đọc ANTHROPIC_API_KEY.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "medium"   # low | medium | high | xhigh | max

    # Bước kiểm chứng sau khi LLM sinh finding:
    #   factcheck = mặc định GIỮ, chỉ bỏ khi diff chứng minh sai (có bằng chứng kiểm được bằng code)
    #   legacy    = prompt cũ "chỉ giữ finding thật sự là vấn đề" (giữ lại để làm đối chứng)
    #   off       = không kiểm chứng
    verify_mode: str = "factcheck"
    # Ngân sách ký tự cho MỘT lần gọi LLM (diff + context của các file trong lô). Vượt -> chia lô,
    # không cắt im lặng. 1 file vượt max_file_chars -> cắt CÓ GHI NHẬN trong report.
    # Nhật ký mỗi lần chạy cho dashboard: auto = firestore nếu có FIREBASE_CREDENTIALS, không thì file JSON cục bộ
    runlog_backend: str = "auto"          # auto | local | firestore | off
    runlog_dir: str = "data/runs"
    firebase_credentials: str = ""        # đường dẫn file service-account JSON
    firestore_collection: str = "hermesqa_runs"
    eval_root: str = "eval"               # dashboard quét các thư mục eval/out* có results
    eval_primary: str = "out-v3-rescored,out-multi-v3-rescored"   # bộ "hiện hành" lên thẻ điểm; còn lại là lịch sử

    max_prompt_chars: int = 60000
    max_file_chars: int = 30000

    # Infra
    redis_url: str = "redis://redis:6379/0"
    database_url: str = "postgresql+psycopg://hermesqa:hermesqa@postgres:5432/hermesqa"
    runner_image: str = "hermesqa-runner:latest"
    workdir: str = "/data/work"
    report_dir: str = "/data/reports"

    # Policy defaults
    max_comments_per_pr: int = 15
    min_confidence: float = 0.6
    min_severity_to_comment: str = "medium"
    block_on_critical: bool = False

    @model_validator(mode="after")
    def _apply_llm_profile(self):
        if self.llm_profile:
            self.use_profile(self.llm_profile)
        return self

    def llm_profiles(self) -> dict[str, dict[str, str]]:
        """Mọi profile đã khai báo: {tên: {base_url, api_key, model, backend?}}. Chỉ tính profile có MODEL.

        Nguồn: khoá thừa đọc từ .env (model_extra) và biến môi trường thật (docker-compose `env_file`
        đưa .env vào os.environ chứ không qua dotenv, nên phải quét cả hai; os.environ thắng)."""
        found: dict[str, dict[str, str]] = {}
        sources = [(k.lower(), v) for k, v in (self.model_extra or {}).items()]
        sources += [(k.lower(), v) for k, v in os.environ.items()]
        for key, value in sources:
            m = PROFILE_KEY_RE.match(key)
            if m and value is not None:
                found.setdefault(m.group(1), {})[m.group(2)] = str(value)
        return {name: p for name, p in found.items() if p.get("model")}

    def use_profile(self, name: str) -> None:
        """Chuyển sang profile LLM `name` (không phân biệt hoa thường). Lỗi rõ ràng nếu chưa khai báo."""
        profiles = self.llm_profiles()
        p = profiles.get(name.lower())
        if not p:
            have = ", ".join(sorted(profiles)) or "chưa có profile nào"
            raise ValueError(f"không có profile LLM '{name}' — cần LLM_{name.upper()}_MODEL (+ _BASE_URL, _API_KEY) trong .env; đã khai báo: {have}")
        self.llm_profile = name.lower()
        self.agent_backend = p.get("backend", "openai_compat")
        self.llm_model = p["model"]
        if p.get("base_url"):
            self.llm_base_url = p["base_url"]
        if p.get("api_key"):
            self.llm_api_key = p["api_key"]

    @property
    def skills_dir(self) -> Path:
        return Path(__file__).parent / "skills"

    @property
    def private_key(self) -> str:
        return Path(self.github_private_key_path).read_text()


settings = Settings()
