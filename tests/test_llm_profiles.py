"""Profile LLM: khai báo nhiều nhà cung cấp (Gemini, Muse Spark...) song song trong .env và chọn bằng
LLM_PROFILE / --profile, để eval so sánh model mà không sửa .env hay đụng pipeline."""
import importlib.util
import os

import pytest

from app.config import Settings, settings

BASE = {"github_app_id": "1", "github_webhook_secret": "x"}


def _clear_profiles(monkeypatch):
    for k in list(os.environ):
        if k.upper().startswith("LLM_"):
            monkeypatch.delenv(k, raising=False)


def test_profiles_from_environment_and_switch(monkeypatch):
    _clear_profiles(monkeypatch)
    monkeypatch.setenv("LLM_MUSE_BASE_URL", "https://api.meta.ai/v1")
    monkeypatch.setenv("LLM_MUSE_API_KEY", "muse-key")
    monkeypatch.setenv("LLM_MUSE_MODEL", "muse-spark-1.3")
    monkeypatch.setenv("LLM_GEMINI_MODEL", "gemini-3.1-flash-lite")
    monkeypatch.setenv("LLM_GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    monkeypatch.setenv("LLM_NOMODEL_BASE_URL", "https://x")          # thiếu MODEL -> không phải profile
    s = Settings(_env_file=None, llm_model="default-model", llm_api_key="default-key", **BASE)
    assert set(s.llm_profiles()) == {"muse", "gemini"}
    assert s.llm_model == "default-model" and s.llm_profile == ""   # chưa chọn -> không đổi hành vi cũ

    s.use_profile("MUSE")
    assert (s.llm_profile, s.llm_base_url, s.llm_api_key, s.llm_model) == \
        ("muse", "https://api.meta.ai/v1", "muse-key", "muse-spark-1.3")
    assert s.agent_backend == "openai_compat"

    s.use_profile("gemini")                                            # profile không có API key -> giữ key đang có
    assert s.llm_model == "gemini-3.1-flash-lite" and s.llm_api_key == "muse-key"

    with pytest.raises(ValueError, match="không có profile LLM 'claude'.*muse"):
        s.use_profile("claude")


def test_llm_profile_env_applies_at_startup(monkeypatch):
    _clear_profiles(monkeypatch)
    monkeypatch.setenv("LLM_MUSE_MODEL", "muse-spark-1.2")
    monkeypatch.setenv("LLM_MUSE_API_KEY", "k")
    monkeypatch.setenv("LLM_PROFILE", "muse")
    s = Settings(_env_file=None, **BASE)
    assert s.llm_model == "muse-spark-1.2" and s.llm_api_key == "k"
    monkeypatch.setenv("LLM_PROFILE", "khong-co")
    with pytest.raises(ValueError):
        Settings(_env_file=None, **BASE)


def test_profiles_from_dotenv_file(tmp_path, monkeypatch):
    """docker-compose đưa .env vào os.environ, còn chạy tay thì pydantic đọc file .env: cả hai đường đều phải thấy profile."""
    _clear_profiles(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("GITHUB_APP_ID=1\nGITHUB_WEBHOOK_SECRET=x\nLLM_MODEL=gemini-3.1-flash-lite\n"
                   "LLM_MUSE_MODEL=muse-spark-1.3\nLLM_MUSE_BASE_URL=https://api.meta.ai/v1\nLLM_MUSE_API_KEY=mk\n",
                   encoding="utf-8")
    s = Settings(_env_file=str(env))
    assert s.llm_profiles()["muse"]["model"] == "muse-spark-1.3"
    assert s.llm_model == "gemini-3.1-flash-lite"
    s.use_profile("muse")
    assert s.llm_base_url == "https://api.meta.ai/v1"


def test_backend_uses_selected_profile(monkeypatch):
    """OpenAICompatBackend đọc settings lúc khởi tạo -> sau use_profile() nó phải trỏ sang nhà cung cấp mới."""
    from app.review.agent import OpenAICompatBackend
    _clear_profiles(monkeypatch)
    for f in ("llm_profile", "llm_base_url", "llm_api_key", "llm_model", "agent_backend"):
        monkeypatch.setattr(settings, f, getattr(settings, f))      # tự khôi phục sau test
    monkeypatch.setenv("LLM_MUSE_MODEL", "muse-spark-1.3")
    monkeypatch.setenv("LLM_MUSE_BASE_URL", "https://api.meta.ai/v1")
    monkeypatch.setenv("LLM_MUSE_API_KEY", "mk")
    settings.use_profile("muse")
    b = OpenAICompatBackend()
    assert str(b.client.base_url).startswith("https://api.meta.ai/v1")
    assert b.client.api_key == "mk" and settings.llm_model == "muse-spark-1.3"


def test_run_eval_passes_profile_to_subprocess():
    spec = importlib.util.spec_from_file_location("hqa_run_eval", os.path.join(os.path.dirname(__file__), "..", "eval", "run_eval.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    base = mod.eval_env(None)
    assert base.get("LLM_PROFILE") == os.environ.get("LLM_PROFILE")   # không có profile -> không chèn gì thêm
    env = mod.eval_env("muse")
    assert env["LLM_PROFILE"] == "muse" and env["MIN_CONFIDENCE"] == "0.0"
