"""A saved source's table: the clean rows of every load, upserted on the target's unique key.

Each source (customer + source name) has its own DuckDB file under ``<data dir>/sources/``.
A load copies a run's ``stg_<table>`` rows into it with ``loaded_at`` and ``run_id``; a row whose
key is already there is replaced, so loading the same file twice changes nothing but the
lineage columns.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import duckdb
from pydantic import BaseModel

from tools.dbt_runner import model_names
from tools.warehouse import Warehouse, quote_ident


class LoadResult(BaseModel):
    table: str
    inserted: int  # rows whose key was new
    updated: int  # rows that replaced an earlier load's row with the same key
    total_rows: int  # rows in the table after the load


def source_db_path(data_dir: Path, customer_name: str, source_name: str) -> Path:
    """Where a source's table lives; the name is readable and unique per (customer, source)."""
    label = f"{customer_name}-{source_name}"
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:60] or "source"
    digest = hashlib.sha256(label.encode()).hexdigest()[:8]
    return data_dir / "sources" / f"{slug}-{digest}" / "warehouse.duckdb"


def upsert_rows(target: Path, run_wh: Warehouse, schema: dict[str, Any], run_id: str) -> LoadResult:
    """Copy a built run's clean rows into the source table, replacing rows with the same key."""
    table = quote_ident(schema["table"])
    columns = [c["name"] for c in schema["columns"]]
    key = next((c["name"] for c in schema["columns"] if c.get("unique")), None)
    staged = f"run.main.{quote_ident(model_names(schema['table'])['staging_model'])}"
    definitions = [f"{quote_ident(c['name'])} {c['type'].upper()}" for c in schema["columns"]]
    definitions += ["loaded_at TIMESTAMP NOT NULL", "run_id VARCHAR NOT NULL"]
    if key:
        definitions.append(f"PRIMARY KEY ({quote_ident(key)})")
    names = ", ".join(quote_ident(c) for c in columns)

    target.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(target))
    try:
        con.execute(f"CREATE TABLE IF NOT EXISTS main.{table} ({', '.join(definitions)})")
        run_path = str(run_wh.path.resolve()).replace("'", "''")
        con.execute(f"ATTACH '{run_path}' AS run (READ_ONLY)")
        try:
            updated = 0
            if key:
                [(updated,)] = con.execute(
                    f"SELECT count(*) FROM {staged} s JOIN main.{table} t "
                    f"ON s.{quote_ident(key)} = t.{quote_ident(key)}"
                ).fetchall()
            [(staged_rows,)] = con.execute(f"SELECT count(*) FROM {staged}").fetchall()
            insert = (
                f"INSERT INTO main.{table} ({names}, loaded_at, run_id) "
                f"SELECT {names}, current_timestamp, ? FROM {staged}"
            )
            if key:
                assignments = ", ".join(
                    f"{quote_ident(c)} = excluded.{quote_ident(c)}"
                    for c in [*columns, "loaded_at", "run_id"]
                    if c != key
                )
                insert += f" ON CONFLICT ({quote_ident(key)}) DO UPDATE SET {assignments}"
            con.execute("BEGIN TRANSACTION")
            con.execute(insert, [run_id])
            con.execute("COMMIT")
        finally:
            con.execute("DETACH run")
        [(total,)] = con.execute(f"SELECT count(*) FROM main.{table}").fetchall()
    finally:
        con.close()
    return LoadResult(
        table=schema["table"], inserted=staged_rows - updated, updated=updated, total_rows=total
    )


def table_rows(target: Path, table: str) -> int | None:
    """Rows in a source table, or None when nothing was loaded yet."""
    if not target.is_file():
        return None
    con = duckdb.connect(str(target), read_only=True)
    try:
        [(exists,)] = con.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE schema_name = 'main' AND table_name = ?",
            [table],
        ).fetchall()
        if not exists:
            return None
        [(n,)] = con.execute(f"SELECT count(*) FROM main.{quote_ident(table)}").fetchall()
        return n
    finally:
        con.close()
