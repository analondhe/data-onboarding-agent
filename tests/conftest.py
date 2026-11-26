from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

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
