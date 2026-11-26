"""Settings read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_reasoning_effort: str | None  # sent only when set, e.g. "none" to turn off thinking
    llm_timeout_seconds: float
    database_url: str
    mlflow_tracking_uri: str
    data_dir: Path
    review_confidence_threshold: float
    agent_max_steps: int

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        data_dir = Path(env("DATA_DIR", "data"))
        return cls(
            llm_base_url=env("LLM_BASE_URL", "http://localhost:11434/v1"),
            llm_model=env("LLM_MODEL", ""),
            llm_api_key=env("LLM_API_KEY", "not-needed-for-ollama"),
            llm_reasoning_effort=env("LLM_REASONING_EFFORT") or None,
            llm_timeout_seconds=float(env("LLM_TIMEOUT_SECONDS", "300")),
            database_url=env("DATABASE_URL", f"sqlite:///{data_dir / 'app.db'}"),
            mlflow_tracking_uri=env("MLFLOW_TRACKING_URI", f"sqlite:///{data_dir / 'mlflow.db'}"),
            data_dir=data_dir,
            review_confidence_threshold=float(env("REVIEW_CONFIDENCE_THRESHOLD", "0.8")),
            agent_max_steps=int(env("AGENT_MAX_STEPS", "25")),
        )
