from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # GitHub App
    github_app_id: str
    github_webhook_secret: str
    github_private_key_path: str = "/app/secrets/github-app.pem"

    # Agent
    agent_backend: str = "openai_compat"  # openai_compat | hermes_cli
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = "anthropic/claude-sonnet-4"
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

    @property
    def skills_dir(self) -> Path:
        return Path(__file__).parent / "skills"

    @property
    def private_key(self) -> str:
        return Path(self.github_private_key_path).read_text()


settings = Settings()
