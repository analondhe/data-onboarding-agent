"""Dependency checks for GET /health: the database, the model endpoint, the tools server."""

from __future__ import annotations

import json
import urllib.request
from urllib.parse import urlsplit

import openai
from sqlalchemy import Engine, text

from agent.settings import Settings

TIMEOUT_SECONDS = 5


def check_database(engine: Engine) -> str:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:
        return f"error: {type(exc).__name__}: {exc}"


def check_model(settings: Settings) -> str:
    """The endpoint answers /models and, when LLM_MODEL is set, it is configured."""
    if not settings.llm_model:
        return "error: LLM_MODEL is not set"
    try:
        client = openai.OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=TIMEOUT_SECONDS,
            max_retries=0,
        )
        client.models.list()
        return "ok"
    except Exception as exc:
        return f"error: {type(exc).__name__}: {exc}"


def check_tools(settings: Settings) -> str:
    if not settings.tools_url:
        return "ok (in-process)"
    parts = urlsplit(settings.tools_url)
    url = f"{parts.scheme}://{parts.netloc}/health"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read())
        return "ok" if body.get("status") == "ok" else f"error: {body}"
    except Exception as exc:
        return f"error: {type(exc).__name__}: {exc}"
