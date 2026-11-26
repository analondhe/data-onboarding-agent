from __future__ import annotations

from pathlib import Path

from agent.settings import Settings


def test_env_file_fills_gaps_but_real_environment_wins(tmp_path: Path, monkeypatch):
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "# OpenRouter\n"
        "LLM_BASE_URL=https://openrouter.ai/api/v1\n"
        'LLM_API_KEY="sk-or-secret"\n'
        "export LLM_MODEL=from-file\n"
        "not a setting\n"
    )
    monkeypatch.setenv("LLM_MODEL", "from-environment")
    settings = Settings.from_env()
    assert settings.llm_base_url == "https://openrouter.ai/api/v1"
    assert settings.llm_api_key == "sk-or-secret"
    assert settings.llm_model == "from-environment"
