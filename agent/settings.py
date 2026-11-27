"""Settings read from environment variables (see .env.example).

A ``.env`` file in the working directory fills in variables that are not already set, so a
real environment variable always wins over the file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_env_file(path: Path = Path(".env")) -> None:
    """Set KEY=VALUE lines from ``path`` as environment defaults; comments and blanks skipped."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip().removeprefix("export ").strip(), value)


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_reasoning_effort: str | None  # sent only when set, e.g. "none" to turn off thinking
    llm_timeout_seconds: float
    llm_retry_backoff_seconds: float  # first retry wait; doubles on each retry
    database_url: str
    mlflow_tracking_uri: str
    data_dir: Path
    review_confidence_threshold: float
    agent_max_steps: int
    build_max_repairs: int
    tools_url: str | None = None  # MCP tools server; unset means call the tools in-process
    max_upload_mb: int = 1024
    mlflow_ui_url: str | None = None  # base URL for trace links in reports
    cors_origins: tuple[str, ...] = ("http://localhost:3000",)  # browsers allowed to call the API

    @classmethod
    def from_env(cls) -> Settings:
        load_env_file()
        env = os.environ.get
        data_dir = Path(env("DATA_DIR", "data"))
        return cls(
            llm_base_url=env("LLM_BASE_URL", "http://localhost:11434/v1"),
            llm_model=env("LLM_MODEL", ""),
            llm_api_key=env("LLM_API_KEY", "not-needed-for-ollama"),
            llm_reasoning_effort=env("LLM_REASONING_EFFORT") or None,
            llm_timeout_seconds=float(env("LLM_TIMEOUT_SECONDS", "300")),
            llm_retry_backoff_seconds=float(env("LLM_RETRY_BACKOFF_SECONDS", "5")),
            database_url=env("DATABASE_URL", f"sqlite:///{data_dir / 'app.db'}"),
            mlflow_tracking_uri=env("MLFLOW_TRACKING_URI", f"sqlite:///{data_dir / 'mlflow.db'}"),
            data_dir=data_dir,
            review_confidence_threshold=float(env("REVIEW_CONFIDENCE_THRESHOLD", "0.8")),
            agent_max_steps=int(env("AGENT_MAX_STEPS", "25")),
            build_max_repairs=int(env("BUILD_MAX_REPAIRS", "3")),
            tools_url=env("TOOLS_URL") or None,
            max_upload_mb=int(env("MAX_UPLOAD_MB", "1024")),
            mlflow_ui_url=env("MLFLOW_UI_URL") or None,
            cors_origins=tuple(
                o.strip()
                for o in env("CORS_ORIGINS", "http://localhost:3000").split(",")
                if o.strip()
            ),
        )
