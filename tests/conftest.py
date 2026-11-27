from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from agent.settings import Settings
from datagen.generate import Tables, format_value, generate
from datagen.schemas import TABLES, column_names, load_schema
from tools.warehouse import FileInfo, Warehouse, load_file


@pytest.fixture(scope="session")
def tables() -> Tables:
    return generate(seed=7, customers=400)


@pytest.fixture(scope="session")
def schemas() -> dict[str, dict]:
    return {name: load_schema(name) for name in TABLES}


@pytest.fixture(scope="session")
def clean_rows(tables: Tables, schemas: dict[str, dict]) -> dict[str, list[dict[str, str]]]:
    """Clean rows as CSV strings, the form corrupt_table reads from clean CSVs."""
    return {
        name: [
            {col: format_value(row[col]) for col in column_names(schemas[name])}
            for row in getattr(tables, name)[:300]
        ]
        for name in TABLES
    }


@pytest.fixture
def wh(tmp_path: Path) -> Warehouse:
    return Warehouse.for_run("test-run", tmp_path / "data")


@pytest.fixture
def load_csv(wh: Warehouse, tmp_path: Path) -> Callable[..., FileInfo]:
    """Write CSV text to a file and load it into the run's warehouse."""

    def load(text: str, name: str = "source.csv") -> FileInfo:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return load_file(wh, path)

    return load


@pytest.fixture(scope="session")
def tracking_uri(tmp_path_factory) -> str:
    """One MLflow store for the session: creating its tables takes about a second."""
    return f"sqlite:///{tmp_path_factory.mktemp('mlflow') / 'mlflow.db'}"


@pytest.fixture
def settings(tmp_path: Path, tracking_uri: str) -> Settings:
    return Settings(
        llm_base_url="http://unused",
        llm_model="unused",
        llm_api_key="unused",
        llm_reasoning_effort=None,
        llm_timeout_seconds=1,
        llm_retry_backoff_seconds=0,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        mlflow_tracking_uri=tracking_uri,
        data_dir=tmp_path / "data",
        review_confidence_threshold=0.8,
        agent_max_steps=8,
        build_max_repairs=3,
    )
