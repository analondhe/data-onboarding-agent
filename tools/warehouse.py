"""Per-run DuckDB warehouse: connections, raw file loading, and the file registry.

Each run owns ``<DATA_DIR>/runs/<run_id>/warehouse.duckdb``. Source files are loaded into
``raw.<table>`` with every column as text plus a ``_row`` column holding the 1-based source
row number. Tool metadata (loaded files, stored column profiles) lives in the ``meta`` schema.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
from pydantic import BaseModel

ROW_COLUMN = "_row"
SUPPORTED_SUFFIXES = {".csv": "csv", ".tsv": "csv", ".txt": "csv", ".parquet": "parquet"}
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

_SCHEMA_DDL = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS meta;
CREATE TABLE IF NOT EXISTS meta.files (
    file_id VARCHAR PRIMARY KEY,
    filename VARCHAR NOT NULL,
    table_name VARCHAR NOT NULL UNIQUE,
    file_format VARCHAR NOT NULL,
    row_count BIGINT NOT NULL,
    sha256 VARCHAR NOT NULL,
    loaded_at TIMESTAMP NOT NULL DEFAULT current_timestamp
);
CREATE TABLE IF NOT EXISTS meta.column_profiles (
    file_id VARCHAR NOT NULL,
    column_name VARCHAR NOT NULL,
    position INTEGER NOT NULL,
    profile JSON NOT NULL,
    PRIMARY KEY (file_id, column_name)
);
"""


class ToolError(Exception):
    """A problem the caller (usually the model) can fix; the message is shown to it as is."""


class FileInfo(BaseModel):
    file_id: str
    filename: str
    table_name: str
    row_count: int
    sha256: str


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class Warehouse:
    run_id: str
    path: Path

    @classmethod
    def for_run(cls, run_id: str, data_dir: Path | str | None = None) -> Warehouse:
        if not _RUN_ID.fullmatch(run_id):
            raise ToolError(
                f"Invalid run_id {run_id!r}: use letters, digits, '-' or '_' (max 64 chars)"
            )
        base = Path(data_dir if data_dir is not None else os.environ.get("DATA_DIR", "data"))
        return cls(run_id, base / "runs" / run_id / "warehouse.duckdb")

    @contextmanager
    def connect(self, *, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
        """Open a short-lived connection.

        Read-only connections also disable external access, so SQL written by the model can
        read only this warehouse: no files, no network, no attaching other databases. They are
        capped at ``TOOL_MEMORY_LIMIT``. DuckDB refuses a read-only and a read-write connection
        to the same file at the same time, so callers must not nest them.
        """
        if read_only:
            if not self.path.exists():
                raise ToolError(f"Run {self.run_id!r} has no loaded files yet")
            con = duckdb.connect(
                str(self.path),
                read_only=True,
                config={
                    "enable_external_access": False,
                    "memory_limit": os.environ.get("TOOL_MEMORY_LIMIT", "2GB"),
                },
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            con = duckdb.connect(str(self.path))
            con.execute(_SCHEMA_DDL)
        try:
            yield con
        finally:
            con.close()


def run_guarded(
    con: duckdb.DuckDBPyConnection, sql: str
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Run model-influenced SQL with a time limit; return (column names, rows).

    The query is interrupted after ``TOOL_QUERY_TIMEOUT_SECONDS``. Timeouts and running out of
    memory become ToolErrors the model can act on; other DuckDB errors propagate.
    """
    timeout = float(os.environ.get("TOOL_QUERY_TIMEOUT_SECONDS", "60"))
    timer = threading.Timer(timeout, con.interrupt)
    timer.start()
    try:
        result = con.execute(sql)
        return [d[0] for d in result.description], result.fetchall()
    except duckdb.InterruptException as exc:
        raise ToolError(
            f"The query took longer than {timeout:g} seconds and was stopped. "
            "Simplify the expression."
        ) from exc
    except duckdb.OutOfMemoryException as exc:
        raise ToolError(
            "The query ran out of memory and was stopped. Avoid building very large values."
        ) from exc
    finally:
        timer.cancel()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_name(stem: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_") or "file"
    if base[0].isdigit():
        base = f"f_{base}"
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}_{n}"
    return name


def _file_from_row(row: tuple) -> FileInfo:
    file_id, filename, table_name, row_count, sha256 = row
    return FileInfo(
        file_id=file_id,
        filename=filename,
        table_name=table_name,
        row_count=row_count,
        sha256=sha256,
    )


_FILE_COLUMNS = "file_id, filename, table_name, row_count, sha256"


def load_file(wh: Warehouse, path: Path) -> FileInfo:
    """Load a source file into ``raw`` with every column as text.

    Loading identical content again returns the existing file and creates nothing new.
    """
    path = Path(path)
    if not path.is_file():
        raise ToolError(f"File not found: {path}")
    file_format = SUPPORTED_SUFFIXES.get(path.suffix.lower())
    if file_format is None:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise ToolError(f"Unsupported file type {path.suffix!r} for {path.name}; use {supported}")
    if path.stat().st_size == 0:
        raise ToolError(f"{path.name} is empty")

    sha256 = _sha256(path)
    file_id = sha256[:16]
    with wh.connect() as con:
        existing = con.execute(
            f"SELECT {_FILE_COLUMNS} FROM meta.files WHERE file_id = ?", [file_id]
        ).fetchall()
        if existing:
            return _file_from_row(existing[0])

        taken = {row[0] for row in con.execute("SELECT table_name FROM meta.files").fetchall()}
        table = _table_name(path.stem, taken)
        if file_format == "csv":
            source = f"read_csv({_quote_literal(str(path))}, header = true, all_varchar = true)"
            columns = "*"
        else:
            source = f"read_parquet({_quote_literal(str(path))})"
            columns = "COLUMNS(*)::VARCHAR"
        con.execute("BEGIN")
        try:
            names = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()]
            if ROW_COLUMN in names:
                raise ToolError(f"{path.name} has a column named {ROW_COLUMN!r}, which is reserved")
            con.execute(
                f"CREATE TABLE raw.{quote_ident(table)} AS "
                f"SELECT row_number() OVER () AS {ROW_COLUMN}, {columns} FROM {source}"
            )
            [(row_count,)] = con.execute(
                f"SELECT count(*) FROM raw.{quote_ident(table)}"
            ).fetchall()
            if row_count == 0:
                raise ToolError(f"{path.name} has a header but no data rows")
            con.execute(
                "INSERT INTO meta.files (file_id, filename, table_name, file_format, row_count, "
                "sha256) VALUES (?, ?, ?, ?, ?, ?)",
                [file_id, path.name, table, file_format, row_count, sha256],
            )
            con.execute("COMMIT")
        except duckdb.Error as exc:
            con.execute("ROLLBACK")
            raise ToolError(f"Could not read {path.name}: {exc}") from exc
        except ToolError:
            con.execute("ROLLBACK")
            raise
    return FileInfo(
        file_id=file_id, filename=path.name, table_name=table, row_count=row_count, sha256=sha256
    )


def loaded_files(con: duckdb.DuckDBPyConnection) -> list[FileInfo]:
    """Every file loaded into this run, in load order."""
    rows = con.execute(
        f"SELECT {_FILE_COLUMNS} FROM meta.files ORDER BY loaded_at, file_id"
    ).fetchall()
    return [_file_from_row(row) for row in rows]


def get_file(con: duckdb.DuckDBPyConnection, file_id: str) -> FileInfo:
    rows = con.execute(
        f"SELECT {_FILE_COLUMNS} FROM meta.files WHERE file_id = ?", [file_id]
    ).fetchall()
    if not rows:
        known = ", ".join(f.file_id for f in loaded_files(con)) or "none"
        raise ToolError(f"Unknown file_id {file_id!r}. Known file ids: {known}")
    return _file_from_row(rows[0])


def source_columns(con: duckdb.DuckDBPyConnection, file: FileInfo) -> list[str]:
    """Column names of the loaded file, in file order, without the ``_row`` column."""
    described = con.execute(f"DESCRIBE raw.{quote_ident(file.table_name)}").fetchall()
    return [row[0] for row in described if row[0] != ROW_COLUMN]


def resolve_column(columns: list[str], requested: str) -> str:
    """Match a column name exactly, then case-insensitively; raise a helpful error otherwise."""
    if requested in columns:
        return requested
    folded = [c for c in columns if c.lower() == requested.lower()]
    if len(folded) == 1:
        return folded[0]
    raise ToolError(f"Unknown column {requested!r}. Columns are: {', '.join(columns)}")
