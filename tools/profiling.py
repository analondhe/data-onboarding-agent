"""Profiling tools: list_files, profile_file, get_column_profile, sample_rows, detect_patterns.

All functions are deterministic over the run's DuckDB warehouse. Raw values are text, so
types are inferred from how many non-null values look like each candidate type.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

import duckdb
from pydantic import BaseModel

from tools.transforms import validate_expression
from tools.warehouse import (
    ROW_COLUMN,
    FileInfo,
    ToolError,
    Warehouse,
    get_file,
    loaded_files,
    quote_ident,
    resolve_column,
    run_guarded,
    source_columns,
)

MAX_SAMPLE_ROWS = 20
MAX_CELL_CHARS = 200
MAX_PATTERNS = 12
TYPE_THRESHOLD = 0.5  # an inferred type must match at least this share of real values

# Text that means "no value". Compared after trim and lower-casing.
NULL_TOKENS = ("null", "none", "nil", "n/a", "na", "#n/a", "nan", "-", "--", "?", "missing")
BOOLEAN_WORDS = ("true", "false", "t", "f", "yes", "no", "y", "n")
_MONTH = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_NUMBER = r"[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?"
_TIMESTAMP = r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?"
# Ordered: the first match names the value's shape.
_NAMED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (label, re.compile(pattern, re.IGNORECASE if ignore_case else 0))
    for label, pattern, ignore_case in [
        ("YYYY-MM-DD", r"\d{4}-\d{2}-\d{2}", False),
        ("YYYY-MM-DD HH:MM:SS", _TIMESTAMP, False),
        ("NN/NN/YYYY", r"\d{1,2}/\d{1,2}/\d{4}", False),
        ("YYYY/MM/DD", r"\d{4}/\d{2}/\d{2}", False),
        ("YYYYMMDD", r"(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])", False),
        ("DD Mon YYYY", rf"\d{{1,2}} {_MONTH}[a-z]* \d{{4}}", False),
        ("DD-Mon-YYYY", rf"\d{{1,2}}-{_MONTH}[a-z]*-\d{{4}}", False),
        ("Mon DD, YYYY", rf"{_MONTH} \d{{1,2}},? \d{{4}}", False),
        ("Month DD, YYYY", rf"{_MONTH}[a-z]+ \d{{1,2}},? \d{{4}}", False),
        ("true/false", r"true|false", True),
        ("yes/no", r"yes|no", True),
        ("Y/N", r"y|n", True),
        ("T/F", r"t|f", True),
        ("integer", r"[+-]?\d+", False),
        ("decimal", r"[+-]?\d+\.\d+", False),
        ("number with thousands separators", _NUMBER, False),
        ("currency symbol + number", rf"[+-]?[$€£]\s?{_NUMBER}", False),
        ("currency code + number", rf"(USD|EUR|GBP|CAD|AUD)\s?{_NUMBER}", True),
        ("number + unit", rf"{_NUMBER}\s+[A-Za-z]+", False),
        ("email", r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", False),
    ]
]  # fmt: skip
_SHAPE_CLASSES = [
    (re.compile(r"[A-Z]+"), "A"),
    (re.compile(r"[a-z]+"), "a"),
    (re.compile(r"\s+"), " "),
]


class TopValue(BaseModel):
    value: str
    count: int


class ColumnProfile(BaseModel):
    column_name: str
    position: int  # 1-based position in the file
    inferred_type: str  # varchar, boolean, integer, decimal, date or timestamp
    type_match_rate: float  # share of non-null, non-null-like values that fit inferred_type
    row_count: int
    null_count: int  # SQL NULL or blank
    null_rate: float
    null_like_count: int  # text such as "N/A" or "NULL" that means no value
    distinct_count: int
    min: str | None
    max: str | None
    top_values: list[TopValue]
    patterns: dict[str, int]


class FileProfile(BaseModel):
    file_id: str
    filename: str
    row_count: int
    columns: list[ColumnProfile]


class SampleRows(BaseModel):
    file_id: str
    where: str | None
    rows: list[dict[str, Any]]


class ColumnPatterns(BaseModel):
    file_id: str
    column: str
    non_null: int
    patterns: dict[str, int]


# --- Patterns ------------------------------------------------------------------------------


def shape_of(value: str) -> str:
    """Name the shape of one non-null value, e.g. 'YYYY-MM-DD' or 'A-999999'."""
    stripped = value.strip()
    if stripped.lower() in NULL_TOKENS:
        label = "null-like text"
    else:
        label = next((name for name, rx in _NAMED_PATTERNS if rx.fullmatch(stripped)), None)
        if label is None:
            label = stripped
            for rx, symbol in _SHAPE_CLASSES:
                label = rx.sub(symbol, label)
            label = re.sub(r"\d", "9", label)
    return f"{label} (padded)" if stripped != value else label


def _resolve_slash_dates(value_counts: list[tuple[str, int]], shapes: Counter[str]) -> None:
    """Rename NN/NN/YYYY to MM/DD/YYYY or DD/MM/YYYY when some value settles the order."""
    first_over_12 = second_over_12 = False
    for value, _ in value_counts:
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/\d{4}", value.strip())
        if match:
            first_over_12 |= int(match[1]) > 12
            second_over_12 |= int(match[2]) > 12
    if second_over_12 and not first_over_12:
        name = "MM/DD/YYYY"
    elif first_over_12 and not second_over_12:
        name = "DD/MM/YYYY"
    else:
        return  # undecidable or contradictory: keep the neutral name
    for label in [s for s in shapes if s.startswith("NN/NN/YYYY")]:
        shapes[label.replace("NN/NN/YYYY", name)] += shapes.pop(label)


def _patterns(value_counts: list[tuple[str, int]]) -> dict[str, int]:
    shapes: Counter[str] = Counter()
    for value, count in value_counts:
        shapes[shape_of(value)] += count
    _resolve_slash_dates(value_counts, shapes)
    ranked = sorted(shapes.items(), key=lambda item: (-item[1], item[0]))
    result = dict(ranked[:MAX_PATTERNS])
    if len(ranked) > MAX_PATTERNS:
        result["other shapes"] = sum(count for _, count in ranked[MAX_PATTERNS:])
    return result


# --- Profiles ------------------------------------------------------------------------------


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def _value_counts(con: duckdb.DuckDBPyConnection, table: str, column: str) -> list[tuple[str, int]]:
    col = quote_ident(column)
    return con.execute(
        f"SELECT {col}, count(*) FROM raw.{quote_ident(table)} "
        f"WHERE nullif(trim({col}), '') IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1"
    ).fetchall()


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + "…"
    return value


def _profile_column(
    con: duckdb.DuckDBPyConnection, file: FileInfo, column: str, position: int
) -> ColumnProfile:
    v = f"trim({quote_ident(column)})"
    real = f"nullif({v}, '') IS NOT NULL AND lower({v}) NOT IN ({_sql_list(NULL_TOKENS)})"
    checks = {
        "boolean": f"lower({v}) IN ({_sql_list(BOOLEAN_WORDS)})",
        "integer": f"regexp_full_match({v}, '[+-]?\\d+')",
        "decimal": f"regexp_full_match({v}, '[+-]?(\\d+(\\.\\d*)?|\\.\\d+)')",
        "date": f"regexp_full_match({v}, '\\d{{4}}-\\d{{2}}-\\d{{2}}') "
        f"AND try_cast({v} AS DATE) IS NOT NULL",
        "timestamp": f"NOT regexp_full_match({v}, '\\d{{4}}-\\d{{2}}-\\d{{2}}') "
        f"AND regexp_full_match({v}, '\\d{{4}}-\\d{{2}}-\\d{{2}}[ T].*') "
        f"AND try_cast({v} AS TIMESTAMP) IS NOT NULL",
    }  # fmt: skip
    type_counts = ", ".join(f"count(*) FILTER ({real} AND {check})" for check in checks.values())
    [row] = con.execute(
        f"SELECT count(*), count(*) FILTER (nullif({v}, '') IS NULL), "
        f"count(*) FILTER ({real}), {type_counts} FROM raw.{quote_ident(file.table_name)}"
    ).fetchall()
    rows, nulls, real_count, *matches = row
    null_like = rows - nulls - real_count

    rates = {
        name: (m / real_count if real_count else 0.0)
        for name, m in zip(checks, matches, strict=True)
    }
    best = max(checks, key=lambda name: rates[name])  # ties keep the earlier, more specific type
    inferred = best if rates[best] >= TYPE_THRESHOLD else "varchar"
    type_rate = rates[best] if inferred != "varchar" else 1.0

    cast = {"integer": "BIGINT", "decimal": "DOUBLE", "date": "DATE", "timestamp": "TIMESTAMP"}
    if inferred in cast:
        typed = f"try_cast({v} AS {cast[inferred]})"
        bounds_sql = f"SELECT min({typed})::VARCHAR, max({typed})::VARCHAR"
    else:
        bounds_sql = f"SELECT min({v}) FILTER ({real}), max({v}) FILTER ({real})"
    [(low, high)] = con.execute(f"{bounds_sql} FROM raw.{quote_ident(file.table_name)}").fetchall()

    value_counts = _value_counts(con, file.table_name, column)
    return ColumnProfile(
        column_name=column,
        position=position,
        inferred_type=inferred,
        type_match_rate=round(type_rate, 4),
        row_count=rows,
        null_count=nulls,
        null_rate=round(nulls / rows, 4) if rows else 0.0,
        null_like_count=null_like,
        distinct_count=len(value_counts),
        min=_truncate(low),
        max=_truncate(high),
        top_values=[TopValue(value=_truncate(val), count=n) for val, n in value_counts[:5]],
        patterns=_patterns(value_counts),
    )


def profile_file(wh: Warehouse, file_id: str) -> FileProfile:
    """Profile every column of a file and store the profiles in the warehouse."""
    with wh.connect() as con:
        file = get_file(con, file_id)
        profiles = [
            _profile_column(con, file, column, position)
            for position, column in enumerate(source_columns(con, file), start=1)
        ]
        con.execute("BEGIN")
        con.execute("DELETE FROM meta.column_profiles WHERE file_id = ?", [file_id])
        con.executemany(
            "INSERT INTO meta.column_profiles VALUES (?, ?, ?, ?)",
            [[file_id, p.column_name, p.position, p.model_dump_json()] for p in profiles],
        )
        con.execute("COMMIT")
    return FileProfile(
        file_id=file.file_id, filename=file.filename, row_count=file.row_count, columns=profiles
    )


def get_column_profile(wh: Warehouse, file_id: str, column: str) -> ColumnProfile:
    """The stored profile of one column; the file is profiled first if it has not been."""
    with wh.connect(read_only=True) as con:
        file = get_file(con, file_id)
        name = resolve_column(source_columns(con, file), column)
        stored = con.execute(
            "SELECT profile FROM meta.column_profiles WHERE file_id = ? AND column_name = ?",
            [file_id, name],
        ).fetchall()
    if stored:
        return ColumnProfile.model_validate(json.loads(stored[0][0]))
    profile = profile_file(wh, file_id)
    return next(c for c in profile.columns if c.column_name == name)


def sample_rows(wh: Warehouse, file_id: str, n: int = 10, where: str | None = None) -> SampleRows:
    """Up to 20 rows spread across the file, optionally filtered by a SQL condition.

    The same arguments always return the same rows. ``_row`` is the 1-based source row number.
    """
    if n < 1:
        raise ToolError("n must be at least 1")
    n = min(n, MAX_SAMPLE_ROWS)
    with wh.connect(read_only=True) as con:
        file = get_file(con, file_id)
        filter_sql = ""
        if where is not None and where.strip():
            filter_sql = f"WHERE ({validate_expression(where, source_columns(con, file))})"
        try:
            names, rows = run_guarded(
                con,
                f"SELECT * FROM (SELECT * FROM raw.{quote_ident(file.table_name)} {filter_sql} "
                f"ORDER BY hash({ROW_COLUMN}) LIMIT {n}) ORDER BY {ROW_COLUMN}",
            )
        except duckdb.Error as exc:
            raise ToolError(f"The where condition failed when run on the data: {exc}") from exc
    return SampleRows(
        file_id=file_id,
        where=where,
        rows=[{k: _truncate(val) for k, val in zip(names, row, strict=True)} for row in rows],
    )


def list_files(wh: Warehouse) -> list[FileInfo]:
    """Every file loaded into this run: ids, names and row counts."""
    if not wh.path.exists():
        return []
    with wh.connect(read_only=True) as con:
        return loaded_files(con)


def detect_patterns(wh: Warehouse, file_id: str, column: str) -> ColumnPatterns:
    """Count the value shapes in one column, e.g. {"YYYY-MM-DD": 9120, "MM/DD/YYYY": 880}."""
    with wh.connect(read_only=True) as con:
        file = get_file(con, file_id)
        name = resolve_column(source_columns(con, file), column)
        value_counts = _value_counts(con, file.table_name, name)
    return ColumnPatterns(
        file_id=file_id,
        column=name,
        non_null=sum(count for _, count in value_counts),
        patterns=_patterns(value_counts),
    )
