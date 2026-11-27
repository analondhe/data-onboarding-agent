"""Load target schema definitions from ``schemas/*.yml``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
TABLES = ("customers", "subscriptions", "invoices")  # generated Acme Cloud tables with eval cases
TARGETS = (*TABLES, "service_requests")  # every target table a run can load into (311 last)


def load_schema(name: str, schema_dir: Path = SCHEMA_DIR) -> dict[str, Any]:
    """Return the target schema ``name`` as a plain dict."""
    path = schema_dir / f"{name}.yml"
    if not path.is_file():
        raise FileNotFoundError(f"Unknown target schema {name!r}: {path} does not exist")
    with path.open(encoding="utf-8") as fh:
        schema = yaml.safe_load(fh)
    if schema.get("table") != name:
        raise ValueError(f"{path} declares table {schema.get('table')!r}, expected {name!r}")
    return schema


def key_column(schema: dict[str, Any]) -> str:
    """Return the name of the table's unique key column."""
    for column in schema["columns"]:
        if column.get("unique"):
            return column["name"]
    raise ValueError(f"Schema {schema['table']!r} has no unique column")


def column_names(schema: dict[str, Any]) -> list[str]:
    return [column["name"] for column in schema["columns"]]
