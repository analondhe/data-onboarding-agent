from __future__ import annotations

import pytest

from tools.profiling import (
    MAX_PATTERNS,
    detect_patterns,
    get_column_profile,
    profile_file,
    sample_rows,
    shape_of,
)
from tools.transforms import UnsafeSQLError
from tools.warehouse import ToolError


def column(profile, name):
    return next(c for c in profile.columns if c.column_name == name)


def csv_of(**columns: list[str]) -> str:
    """Build CSV text from equal-length columns; '' becomes an empty field (NULL)."""
    names = list(columns)
    rows = zip(*columns.values(), strict=True)
    lines = [",".join(names)] + [",".join(f'"{v}"' if v else "" for v in row) for row in rows]
    return "\n".join(lines) + "\n"


def test_counts_nulls_blanks_null_like_and_distinct_values(wh, load_csv):
    file = load_csv(csv_of(v=["a", "", "  ", "N/A", "NULL", "b", "a"]))
    [col] = profile_file(wh, file.file_id).columns
    assert col.row_count == 7
    assert col.null_count == 2  # empty and whitespace-only
    assert col.null_rate == pytest.approx(2 / 7, abs=1e-4)
    assert col.null_like_count == 2  # N/A and NULL
    assert col.distinct_count == 4
    assert [(t.value, t.count) for t in col.top_values] == [
        ("a", 2),
        ("N/A", 1),
        ("NULL", 1),
        ("b", 1),
    ]
    assert col.patterns == {"a": 3, "null-like text": 2}


def test_infers_types_bounds_and_match_rates(wh, load_csv):
    file = load_csv(
        csv_of(
            ints=["1", "2", "-3", "4"],
            decimals=["1.5", "2", "3.25", "x"],
            bools=["true", "False", "yes", "N"],
            dates=["2024-01-05", "2024-02-30", "2023-12-31", "03/01/2024"],
            stamps=["2024-01-01 10:00:00", "2024-01-02T11:30:00", "x", "y"],
            text=["abc", "1", "def", "ghi"],
            sparse_dates=["2024-01-01", "N/A", "NULL", "2024-01-02"],
        )
    )
    profile = profile_file(wh, file.file_id)
    expected = {
        "ints": ("integer", 1.0, "-3", "4"),
        "decimals": ("decimal", 0.75, "1.5", "3.25"),
        "bools": ("boolean", 1.0, "False", "yes"),
        "dates": ("date", 0.5, "2023-12-31", "2024-01-05"),
        "stamps": ("timestamp", 0.5, "2024-01-01 10:00:00", "2024-01-02 11:30:00"),
        "text": ("varchar", 1.0, "1", "ghi"),
        "sparse_dates": ("date", 1.0, "2024-01-01", "2024-01-02"),
    }
    for name, (inferred, rate, low, high) in expected.items():
        col = column(profile, name)
        assert (col.inferred_type, col.type_match_rate, col.min, col.max) == (
            inferred,
            rate,
            low,
            high,
        ), name
    assert [c.position for c in profile.columns] == list(range(1, 8))


def test_mostly_unparseable_column_is_varchar(wh, load_csv):
    file = load_csv(csv_of(v=["1", "two", "three", "four"]))
    [col] = profile_file(wh, file.file_id).columns
    assert col.inferred_type == "varchar"


def test_profiles_are_stored_and_read_back(wh, load_csv):
    file = load_csv(csv_of(Amount=["1", "2"], Name=["x", "y"]))
    stored = get_column_profile(wh, file.file_id, "amount")  # profiles the file on first use
    assert stored.column_name == "Amount"
    assert stored == column(profile_file(wh, file.file_id), "Amount")
    assert get_column_profile(wh, file.file_id, "Amount") == stored


def test_unknown_file_and_column_are_tool_errors(wh, load_csv):
    file = load_csv(csv_of(a=["1"]))
    with pytest.raises(ToolError, match=r"Unknown file_id 'nope'\. Known file ids: "):
        profile_file(wh, "nope")
    with pytest.raises(ToolError, match=r"Unknown column 'b'\. Columns are: a"):
        get_column_profile(wh, file.file_id, "b")
    with pytest.raises(ToolError, match="Unknown column"):
        detect_patterns(wh, file.file_id, "b")


@pytest.mark.parametrize(
    ("value", "shape"),
    [
        ("2024-03-01", "YYYY-MM-DD"),
        ("2024-03-01 10:00:00", "YYYY-MM-DD HH:MM:SS"),
        ("03/01/2024", "NN/NN/YYYY"),
        ("2024/03/01", "YYYY/MM/DD"),
        ("20240301", "YYYYMMDD"),
        ("01 Mar 2024", "DD Mon YYYY"),
        ("01-Mar-2024", "DD-Mon-YYYY"),
        ("Mar 1, 2024", "Mon DD, YYYY"),
        ("March 01, 2024", "Month DD, YYYY"),
        ("TRUE", "true/false"),
        ("yes", "yes/no"),
        ("N", "Y/N"),
        ("f", "T/F"),
        ("42", "integer"),
        ("-12.5", "decimal"),
        ("1,200", "number with thousands separators"),
        ("$1,200.00", "currency symbol + number"),
        ("USD 12.00", "currency code + number"),
        ("5 units", "number + unit"),
        ("ann@example.com", "email"),
        ("N/A", "null-like text"),
        ("CUS-000001", "A-999999"),
        ("cus-000001", "a-999999"),
        ("Pro", "Aa"),
        ("Miller, Henderson and Johnson", "Aa, Aa a Aa"),
        (" pro ", "a (padded)"),
        ("2024-03-01  ", "YYYY-MM-DD (padded)"),
    ],
)
def test_shape_of(value, shape):
    assert shape_of(value) == shape


def test_detect_patterns_matches_the_spec_example(wh, load_csv):
    dates = ["2024-03-01"] * 3 + ["03/15/2024", "04/02/2024"] + [""]
    file = load_csv(csv_of(d=dates))
    result = detect_patterns(wh, file.file_id, "D")
    assert result.column == "d"
    assert result.non_null == 5
    assert result.patterns == {"YYYY-MM-DD": 3, "MM/DD/YYYY": 2}


@pytest.mark.parametrize(
    ("values", "label"),
    [
        (["15/03/2024", "02/04/2024"], "DD/MM/YYYY"),
        (["03/04/2024", "05/06/2024"], "NN/NN/YYYY"),
        (["13/04/2024", "04/13/2024"], "NN/NN/YYYY"),
    ],
)
def test_slash_dates_are_named_only_when_the_data_settles_the_order(wh, load_csv, values, label):
    file = load_csv(csv_of(d=values))
    assert detect_patterns(wh, file.file_id, "d").patterns == {label: 2}


def test_rare_shapes_are_summed_into_other(wh, load_csv):
    values = ["x" * n for n in range(1, 4)] + [f"{'9' * n}-" for n in range(1, 20)]
    file = load_csv(csv_of(v=values))
    patterns = detect_patterns(wh, file.file_id, "v").patterns
    assert len(patterns) == MAX_PATTERNS + 1
    assert sum(patterns.values()) == len(values)
    assert patterns["other shapes"] == len(values) - sum(
        n for k, n in patterns.items() if k != "other shapes"
    )


def test_sample_rows_is_deterministic_capped_and_ordered(wh, load_csv):
    file = load_csv(csv_of(n=[str(i) for i in range(30)]))
    first = sample_rows(wh, file.file_id, n=50)
    assert len(first.rows) == 20
    assert first == sample_rows(wh, file.file_id, n=50)
    row_numbers = [r["_row"] for r in first.rows]
    assert row_numbers == sorted(row_numbers)
    assert all(r["n"] == str(r["_row"] - 1) for r in first.rows)
    assert len(sample_rows(wh, file.file_id, n=3).rows) == 3


def test_sample_rows_filters_with_a_safe_condition(wh, load_csv):
    file = load_csv(csv_of(plan=["pro", "PRO", "free", "Pro", "enterprise"]))
    rows = sample_rows(wh, file.file_id, n=10, where="lower(plan) = 'pro'").rows
    assert [r["plan"] for r in rows] == ["pro", "PRO", "Pro"]
    assert (
        sample_rows(wh, file.file_id, n=10, where="  ").rows
        == sample_rows(wh, file.file_id, n=10).rows
    )


def test_sample_rows_refuses_unsafe_conditions_and_bad_n(wh, load_csv):
    file = load_csv(csv_of(plan=["pro"]))
    with pytest.raises(UnsafeSQLError):
        sample_rows(wh, file.file_id, where="1=1; DROP TABLE raw.source")
    with pytest.raises(ToolError, match="n must be at least 1"):
        sample_rows(wh, file.file_id, n=0)
    with pytest.raises(ToolError, match="where condition failed"):
        sample_rows(wh, file.file_id, where="plan::integer > 1")


def test_long_cells_are_truncated_in_samples_and_profiles(wh, load_csv):
    file = load_csv(csv_of(v=["z" * 300]))
    [row] = sample_rows(wh, file.file_id).rows
    assert row["v"] == "z" * 200 + "…"
    [col] = profile_file(wh, file.file_id).columns
    assert col.top_values[0].value == "z" * 200 + "…"
