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
