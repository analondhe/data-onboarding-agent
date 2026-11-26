"""SQL safety checks for model-written expressions, and ``test_transform``.

Model SQL is never run as written. It is parsed with sqlglot, checked to be a single
expression over one source row that references only the file's columns, and the SQL that
sqlglot regenerates from the checked tree is what runs, on a read-only connection with
external access disabled.
"""

from __future__ import annotations

import re

import duckdb
import sqlglot
from pydantic import BaseModel
from sqlglot import exp
from sqlglot.errors import SqlglotError

from tools.warehouse import (
    ROW_COLUMN,
    ToolError,
    Warehouse,
    get_file,
    quote_ident,
    run_guarded,
    source_columns,
)

MAX_SQL_LENGTH = 2000
MAX_FAILING_EXAMPLES = 5

# Node types that would turn an expression into a query, a statement, or a multi-row computation.
_FORBIDDEN_NODES: dict[type[exp.Expression], str] = {
    exp.Query: "subqueries and SELECT statements",
    exp.Subquery: "subqueries",
    exp.Table: "table references",
    exp.Star: "'*'",
    exp.Window: "window functions",
    exp.AggFunc: "aggregate functions",
    exp.Placeholder: "parameters",
    exp.Lambda: "lambda functions",
}
# Functions that read outside the row (files, settings, catalog), are not deterministic, or
# build arbitrarily large values. DuckDB's memory_limit does not cover per-row temporaries,
# so repeat() and list builders are refused here; the query timeout backs this up.
_FORBIDDEN_FUNCTION = re.compile(
    r"(read_|parquet_|sniff_|duckdb_|pragma_|iceberg_|delta_|json_execute|query"
    r"|glob$|getenv$|current_|which_secret|setseed|random|rand$|uuid|gen_random_uuid"
    r"|nextval|currval|now$|today$|get_current|localtime|transaction_timestamp"
    r"|repeat$|range$|generate_series|list_resize|array_resize)"
)
# Target types a transform may produce, keyed by sqlglot type.
_ALLOWED_TYPES = {
    exp.DataType.Type.VARCHAR,
    exp.DataType.Type.TEXT,
    exp.DataType.Type.DATE,
    exp.DataType.Type.TIMESTAMP,
    exp.DataType.Type.TIMESTAMPNTZ,  # sqlglot's name for DuckDB TIMESTAMP
    exp.DataType.Type.BOOLEAN,
    exp.DataType.Type.SMALLINT,
    exp.DataType.Type.INT,
    exp.DataType.Type.BIGINT,
    exp.DataType.Type.FLOAT,
    exp.DataType.Type.DOUBLE,
    exp.DataType.Type.DECIMAL,
}


class UnsafeSQLError(ToolError):
    """The expression was refused before running."""


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


def validate_expression(sql: str, columns: list[str]) -> str:
    """Check a single-row DuckDB expression and return the normalized SQL to execute.

    ``columns`` are the source columns the expression may reference.
    """
    if not sql or not sql.strip():
        raise UnsafeSQLError("The SQL expression is empty")
    if len(sql) > MAX_SQL_LENGTH:
        raise UnsafeSQLError(f"The SQL expression is longer than {MAX_SQL_LENGTH} characters")
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except SqlglotError as exc:
        raise UnsafeSQLError(f"The SQL expression does not parse: {exc}") from exc
    if len(statements) != 1:
        raise UnsafeSQLError(f"Expected exactly one expression, found {len(statements)} statements")
    [tree] = statements
    # Statements (DROP, INSERT, ATTACH, COPY, PRAGMA, SET, ...) are not expressions at all.
    if not isinstance(tree, exp.Condition):
        raise UnsafeSQLError(f"Expected a single expression, got a {tree.key.upper()} statement")

    for node in tree.walk():
        for node_type, label in _FORBIDDEN_NODES.items():
            if isinstance(node, node_type):
                raise UnsafeSQLError(
                    f"Only a single-row expression is allowed; {label} are not: "
                    f"{node.sql('duckdb')}"
                )
        if isinstance(node, exp.Func) and _FORBIDDEN_FUNCTION.match(_function_name(node)):
            raise UnsafeSQLError(f"Function {_function_name(node)}() is not allowed")
        if isinstance(node, exp.Column):
            if node.table:
                raise UnsafeSQLError(f"Use bare column names without a table: {node.sql('duckdb')}")
            if not any(c.lower() == node.name.lower() for c in columns):
                raise UnsafeSQLError(
                    f"Unknown column {node.name!r}. Columns are: {', '.join(columns)}. "
                    'Quote names with spaces or symbols in double quotes, e.g. "Sign Up Dt".'
                )
    return tree.sql(dialect="duckdb", comments=False)


def validate_target_type(target_type: str) -> str:
    """Return the normalized DuckDB type name, or refuse types transforms may not produce."""
    try:
        data_type = exp.DataType.build(target_type, dialect="duckdb")
    except (SqlglotError, ValueError) as exc:
        raise ToolError(f"Unknown target type {target_type!r}") from exc
    if data_type.this not in _ALLOWED_TYPES:
        raise ToolError(
            f"Target type {target_type!r} is not supported; use varchar, date, timestamp, "
            "boolean, integer, bigint, double or decimal(p,s)"
        )
    return data_type.sql(dialect="duckdb")


def referenced_columns(sql: str, columns: list[str]) -> list[str]:
    """Source columns an already-validated expression references, in file order."""
    names = {c.name.lower() for c in sqlglot.parse_one(sql, read="duckdb").find_all(exp.Column)}
    return [c for c in columns if c.lower() in names]


class FailingExample(BaseModel):
    row: int
    input: dict[str, str | None]
    output: str | None


class TransformTest(BaseModel):
    file_id: str
    sql_expression: str  # normalized SQL that was run
    target_type: str
    rows: int
    non_null_input: int  # rows where at least one referenced column has a non-blank value
    converted: int  # of those, rows whose output casts to target_type
    null_output: int  # of those, rows where the expression itself returned NULL
    cast_failed: int  # of those, rows whose output is not NULL but does not cast
    success_rate: float  # converted / non_null_input (1.0 when there is no input)
    failing_examples: list[FailingExample]


def test_transform(
    wh: Warehouse, file_id: str, sql_expression: str, target_type: str
) -> TransformTest:
    """Run an expression over every row of a file and report how often it casts cleanly."""
    target = validate_target_type(target_type)
    with wh.connect(read_only=True) as con:
        file = get_file(con, file_id)
        columns = source_columns(con, file)
        expression = validate_expression(sql_expression, columns)
        inputs = referenced_columns(expression, columns)
        if inputs:
            has_input = " OR ".join(
                f"nullif(trim({quote_ident(c)}), '') IS NOT NULL" for c in inputs
            )
        else:
            has_input = "true"
        evaluated = (
            f"SELECT {ROW_COLUMN}, "
            + "".join(f"{quote_ident(c)}, " for c in inputs)
            + f"({has_input}) AS __has_input, ({expression})::VARCHAR AS __out, "
            f"TRY_CAST(({expression}) AS {target}) IS NOT NULL AS __ok "
            f"FROM raw.{quote_ident(file.table_name)}"
        )
        try:
            _, [(rows, non_null, converted, null_output)] = run_guarded(
                con,
                "SELECT count(*), count(*) FILTER (__has_input), "
                "count(*) FILTER (__has_input AND __ok), "
                "count(*) FILTER (__has_input AND __out IS NULL) "
                f"FROM ({evaluated})",
            )
            _, failing = run_guarded(
                con,
                f"SELECT * FROM ({evaluated}) WHERE __has_input AND NOT __ok "
                f"ORDER BY {ROW_COLUMN} LIMIT {MAX_FAILING_EXAMPLES}",
            )
        except duckdb.Error as exc:
            raise ToolError(
                f"The expression failed when run on the data: {exc}. Use try_cast or "
                "try_strptime so one bad value does not fail every row."
            ) from exc

    examples = [
        FailingExample(
            row=row[0],
            input=dict(zip(inputs, row[1 : 1 + len(inputs)], strict=True)),
            output=row[2 + len(inputs)],
        )
        for row in failing
    ]
    return TransformTest(
        file_id=file_id,
        sql_expression=expression,
        target_type=target,
        rows=rows,
        non_null_input=non_null,
        converted=converted,
        null_output=null_output,
        cast_failed=non_null - converted - null_output,
        success_rate=round(converted / non_null, 4) if non_null else 1.0,
        failing_examples=examples,
    )


# The tool is named by the spec; stop pytest from collecting it when tests import it.
test_transform.__test__ = False  # type: ignore[attr-defined]
