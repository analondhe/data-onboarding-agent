from __future__ import annotations

import pytest

from tools.profiling import sample_rows
from tools.transforms import (
    MAX_FAILING_EXAMPLES,
    UnsafeSQLError,
    test_transform,
    validate_expression,
    validate_target_type,
)
from tools.warehouse import ToolError

COLUMNS = ["x", "d", "Sign Up Dt"]


@pytest.mark.parametrize(
    ("sql", "normalized"),
    [
        (
            "try_strptime(\"Sign Up Dt\", '%m/%d/%Y')::date",
            "CAST(TRY_STRPTIME(\"Sign Up Dt\", '%m/%d/%Y') AS DATE)",
        ),
        ("nullif(trim(x), '')", "NULLIF(TRIM(x), '')"),
        (
            "CASE WHEN lower(x) IN ('n/a', '-') THEN NULL ELSE x END",
            "CASE WHEN LOWER(x) IN ('n/a', '-') THEN NULL ELSE x END",
        ),
        (
            "try_cast(regexp_replace(x, '[$,]', '', 'g') AS decimal(10,2))",
            "TRY_CAST(REGEXP_REPLACE(x, '[$,]', '', 'g') AS DECIMAL(10, 2))",
        ),
        ("X || '-' || D", "X || '-' || D"),
        ("'USD'", "'USD'"),
        ("x -- trailing comment", "x"),
    ],
)
def test_accepts_single_row_expressions(sql, normalized):
    assert validate_expression(sql, COLUMNS) == normalized


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE raw.source",
        "drop table raw.source",
        "x; DROP TABLE raw.source",
        "1; DROP TABLE raw.source",
        "DELETE FROM raw.source",
        "INSERT INTO raw.source VALUES (1)",
        "UPDATE raw.source SET x = 1",
        "CREATE TABLE t AS SELECT 1",
        "ATTACH 'other.db'",
        "COPY raw.source TO 'out.csv'",
        "PRAGMA version",
        "SET threads = 1",
        "INSTALL httpfs",
        "SELECT x FROM raw.source",
        "(SELECT max(x) FROM raw.source)",
        "x || (SELECT 1)",
        "read_csv('/etc/passwd')",
        "read_text('/etc/passwd')",
        "getenv('HOME')",
        "current_setting('threads')",
        "sum(x)",
        "row_number() OVER ()",
        "random()",
        "now()",
        "current_date",
        "nextval('seq')",
        "list_transform([1], y -> y + 1)",
        "source.x",
        "*",
        "unknown_column",
        "x +",
        "",
        "   ",
        "x || '" + "a" * 2000 + "'",
    ],
)
def test_refuses_unsafe_or_multi_row_sql(sql):
    with pytest.raises(UnsafeSQLError):
        validate_expression(sql, COLUMNS)


def test_unknown_column_error_lists_columns_and_quoting_hint():
    with pytest.raises(UnsafeSQLError, match=r"Unknown column 'Sign'.*Columns are: x, d"):
        validate_expression("Sign + 1", COLUMNS)


@pytest.mark.parametrize(
    ("given", "normalized"),
    [
        ("varchar", "TEXT"),
        ("date", "DATE"),
        ("boolean", "BOOLEAN"),
        ("integer", "INT"),
        ("decimal(10,2)", "DECIMAL(10, 2)"),
        ("timestamp", "TIMESTAMP"),
    ],
)
def test_normalizes_target_types(given, normalized):
    assert validate_target_type(given) == normalized


@pytest.mark.parametrize("given", ["blob", "map(varchar, int)", "not a type ("])
def test_refuses_unsupported_target_types(given):
    with pytest.raises(ToolError):
        validate_target_type(given)


DATES = "d\n2024-01-05\n01/05/2024\n\nN/A\ngarbage\n2024-13-01\n"


def test_reports_success_rate_and_failing_examples(wh, load_csv):
    file = load_csv(DATES)
    result = test_transform(
        wh,
        file.file_id,
        "coalesce(try_strptime(d, '%Y-%m-%d'), try_strptime(d, '%m/%d/%Y'))::date",
        "date",
    )
    assert result.rows == 6
    assert result.non_null_input == 5  # the blank row has no input
    assert result.converted == 2
    assert result.null_output == 3
    assert result.cast_failed == 0
    assert result.success_rate == 0.4
    assert [(e.row, e.input, e.output) for e in result.failing_examples] == [
        (4, {"d": "N/A"}, None),
        (5, {"d": "garbage"}, None),
        (6, {"d": "2024-13-01"}, None),
    ]


def test_separates_cast_failures_from_null_outputs(wh, load_csv):
    file = load_csv(DATES)
    result = test_transform(wh, file.file_id, "d", "date")
    assert (result.converted, result.null_output, result.cast_failed) == (1, 0, 4)
    assert result.success_rate == 0.2
    assert result.failing_examples[0].output == "01/05/2024"


def test_failing_examples_are_capped(wh, load_csv):
    file = load_csv("n\n" + "\n".join(f"bad{i}" for i in range(12)) + "\n")
    result = test_transform(wh, file.file_id, "n", "integer")
    assert result.success_rate == 0.0
    assert len(result.failing_examples) == MAX_FAILING_EXAMPLES
    assert [e.row for e in result.failing_examples] == [1, 2, 3, 4, 5]


def test_multi_column_and_constant_expressions(wh, load_csv):
    file = load_csv('"First Name",last\nAnn,Lee\n,Kim\n,\n')
    combined = test_transform(
        wh, file.file_id, "trim(coalesce(\"First Name\", '') || ' ' || last)", "varchar"
    )
    assert (combined.non_null_input, combined.converted) == (2, 2)
    constant = test_transform(wh, file.file_id, "'USD'", "varchar")
    assert (constant.rows, constant.non_null_input, constant.success_rate) == (3, 3, 1.0)


def test_all_blank_input_counts_as_full_success(wh, load_csv):
    file = load_csv('a,b\n1,\n2,""\n')
    result = test_transform(wh, file.file_id, "b::integer", "integer")
    assert (result.non_null_input, result.success_rate) == (0, 1.0)


def test_runtime_errors_become_tool_errors_with_a_hint(wh, load_csv):
    file = load_csv(DATES)
    with pytest.raises(ToolError, match="try_strptime"):
        test_transform(wh, file.file_id, "strptime(d, '%Y-%m-%d')::date", "date")


def test_drop_table_attempt_is_refused_and_data_survives(wh, load_csv):
    file = load_csv(DATES)
    for attempt in ["DROP TABLE raw.source", "d; DROP TABLE raw.source"]:
        with pytest.raises(UnsafeSQLError):
            test_transform(wh, file.file_id, attempt, "varchar")
    assert len(sample_rows(wh, file.file_id, n=20).rows) == file.row_count


def test_unknown_file_is_a_tool_error(wh, load_csv):
    load_csv(DATES)
    with pytest.raises(ToolError, match="Unknown file_id"):
        test_transform(wh, "missing", "d", "date")
