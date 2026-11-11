from __future__ import annotations

import pytest

from datagen.generate import Tables, format_value, generate
from datagen.schemas import TABLES, column_names, load_schema


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
