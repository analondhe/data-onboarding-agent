from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest

from datagen.generate import generate, write_tables
from datagen.schemas import TABLES, column_names, key_column
from evals.corrupt import (
    CORRUPTIONS,
    EXTRA_COLUMNS,
    JUNK_TOKENS,
    MERGE_PAIRS,
    NULL_TOKENS,
    CorruptedTable,
    InfeasibleCorruptionError,
    build_cases,
    corrupt_table,
    plan_cases,
)

DATE_PARSE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d %b %Y", "%Y%m%d", "%B %d, %Y")


def run(clean_rows, schemas, table: str, corruptions: list[str], seed: int = 5) -> CorruptedTable:
    return corrupt_table(clean_rows[table], schemas[table], seed, corruptions)


def source_of(result: CorruptedTable, target: str) -> str:
    [mapping] = [m for m in result.answer_key["mappings"] if m["target_column"] == target]
    return mapping["source_column"]


def messy_values(result: CorruptedTable, source: str) -> list[str]:
    index = result.header.index(source)
    return [row[index] for row in result.rows]


def clean_values(clean_rows, table: str, target: str) -> list[str]:
    return [row[target] for row in clean_rows[table]]


def only_defect(result: CorruptedTable, defect_type: str) -> dict:
    [defect] = [d for d in result.answer_key["defects"] if d["type"] == defect_type]
    return defect


def parse_date(value: str) -> date:
    for fmt in DATE_PARSE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise AssertionError(f"unparseable date {value!r}")


def parse_number(value: str) -> Decimal:
    cleaned = re.sub(r"USD|units|[$,\s]", "", value)
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise AssertionError(f"unparseable number {value!r}") from exc


def parse_bool(value: str) -> str:
    return "true" if value.lower() in {"true", "t", "y", "yes", "1"} else "false"


# --- Invariants for every corruption on every table ---------------------------------------


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("corruption", CORRUPTIONS)
def test_answer_key_accounts_for_every_column_and_row(clean_rows, schemas, table, corruption):
    result = run(clean_rows, schemas, table, [corruption])
    key = result.answer_key

    assert len(result.header) == len(set(result.header))
    assert all(len(row) == len(result.header) for row in result.rows)

    mapped_sources = {m["source_column"] for m in key["mappings"]}
    unmapped = set(key["unmapped_source_columns"])
    assert mapped_sources | unmapped == set(result.header)
    assert not mapped_sources & unmapped

    mapped_targets = [m["target_column"] for m in key["mappings"]]
    assert len(mapped_targets) == len(set(mapped_targets))
    assert set(mapped_targets) | set(key["missing_target_columns"]) == set(
        column_names(schemas[table])
    )

    duplicate_rows = sum(
        d["affected_rows"] for d in key["defects"] if d["type"] == "duplicate_rows"
    )
    assert key["messy_rows"] == len(result.rows) == len(clean_rows[table]) + duplicate_rows
    for defect in key["defects"]:
        assert defect["source_column"] is None or defect["source_column"] in result.header
        assert defect["affected_rows"] > 0


@pytest.mark.parametrize("corruption", CORRUPTIONS)
def test_same_seed_gives_identical_result(clean_rows, schemas, corruption):
    first = run(clean_rows, schemas, "subscriptions", [corruption], seed=99)
    second = run(clean_rows, schemas, "subscriptions", [corruption], seed=99)
    assert first == second


# --- One test per corruption type ----------------------------------------------------------


def test_rename_columns_renames_every_column_and_keeps_values(clean_rows, schemas):
    result = run(clean_rows, schemas, "customers", ["rename_columns"])
    for target in column_names(schemas["customers"]):
        source = source_of(result, target)
        assert source != target
        assert messy_values(result, source) == clean_values(clean_rows, "customers", target)
    assert result.answer_key["defects"] == []


def test_cryptic_names_renames_one_to_three_columns_to_codes(clean_rows, schemas):
    result = run(clean_rows, schemas, "subscriptions", ["cryptic_names"])
    renamed = [m for m in result.answer_key["mappings"] if m["source_column"] != m["target_column"]]
    assert 1 <= len(renamed) <= 3
    for mapping in renamed:
        assert re.fullmatch(r"[a-z0-9_]+", mapping["source_column"])
        values = messy_values(result, mapping["source_column"])
        assert values == clean_values(clean_rows, "subscriptions", mapping["target_column"])


def test_cryptic_names_abbreviates_like_the_spec_example(clean_rows, schemas):
    names = set()
    for seed in range(40):
        result = run(clean_rows, schemas, "customers", ["cryptic_names"], seed=seed)
        names |= {m["source_column"] for m in result.answer_key["mappings"]}
    assert "pln_cd" in names
    assert "sgnp_dt" in names


def test_mixed_date_formats_round_trip_to_clean_dates(clean_rows, schemas):
    result = run(clean_rows, schemas, "invoices", ["mixed_date_formats"])
    defect = only_defect(result, "mixed_date_formats")
    [target] = defect["target_columns"]
    messy = messy_values(result, defect["source_column"])
    clean = clean_values(clean_rows, "invoices", target)

    changed = [i for i, (m, c) in enumerate(zip(messy, clean, strict=True)) if m != c]
    assert len(changed) == defect["affected_rows"]
    assert all(
        parse_date(m) == date.fromisoformat(c) for m, c in zip(messy, clean, strict=True) if c
    )
    assert "MM/DD/YYYY" in defect["details"]["formats"]
    assert sum(defect["details"]["formats"].values()) == sum(1 for c in clean if c)


def test_type_noise_numbers_parse_back_except_junk(clean_rows, schemas):
    result = run(clean_rows, schemas, "subscriptions", ["type_noise"], seed=3)
    defect = only_defect(result, "type_noise")
    [target] = defect["target_columns"]
    messy = messy_values(result, defect["source_column"])
    clean = clean_values(clean_rows, "subscriptions", target)

    changed = [(m, c) for m, c in zip(messy, clean, strict=True) if m != c]
    assert len(changed) == defect["affected_rows"]
    junk = [m for m, _ in changed if m in JUNK_TOKENS]
    assert len(junk) == defect["details"]["unparseable_rows"]
    for m, c in changed:
        if m not in JUNK_TOKENS:
            assert parse_number(m) == Decimal(c), (m, c)


def test_type_noise_booleans_parse_back(clean_rows, schemas):
    result = run(clean_rows, schemas, "customers", ["type_noise"])
    defect = only_defect(result, "type_noise")
    assert defect["target_columns"] == ["is_active"]
    messy = messy_values(result, defect["source_column"])
    clean = clean_values(clean_rows, "customers", "is_active")
    changed = [(m, c) for m, c in zip(messy, clean, strict=True) if m != c]
    assert len(changed) == defect["affected_rows"]
    assert all(parse_bool(m) == c for m, c in changed if m not in JUNK_TOKENS)


def test_duplicates_repeat_existing_rows(clean_rows, schemas):
    result = run(clean_rows, schemas, "invoices", ["duplicates"])
    defect = only_defect(result, "duplicate_rows")
    clean = clean_rows["invoices"]
    assert defect["affected_rows"] == round(len(clean) * 0.03)
    assert (
        defect["details"]["exact"] + defect["details"]["with_casing_or_whitespace"]
        == (defect["affected_rows"])
    )

    def normalize(row: list[str]) -> tuple[str, ...]:
        return tuple(v.strip().upper() for v in row)

    clean_by_key = {row["invoice_id"]: normalize(list(row.values())) for row in clean}
    key_index = result.header.index(defect["source_column"])
    keys = Counter(row[key_index].strip().upper() for row in result.rows)
    assert set(keys) == set(clean_by_key)
    assert sum(n - 1 for n in keys.values()) == defect["affected_rows"]
    # Every messy row, duplicate or not, matches its clean row once casing/whitespace is ignored.
    for row in result.rows:
        assert normalize(row) == clean_by_key[row[key_index].strip().upper()]


def test_null_variants_only_touch_null_cells(clean_rows, schemas):
    result = run(clean_rows, schemas, "subscriptions", ["null_variants"])
    defect = only_defect(result, "null_variants")
    assert defect["target_columns"] == ["canceled_at"]
    messy = messy_values(result, defect["source_column"])
    clean = clean_values(clean_rows, "subscriptions", "canceled_at")
    for m, c in zip(messy, clean, strict=True):
        assert m in NULL_TOKENS if c == "" else m == c
    assert sum(1 for m, c in zip(messy, clean, strict=True) if m != c) == defect["affected_rows"]
    assert sum(defect["details"]["tokens"].values()) == clean.count("")


def test_value_drift_variants_map_back_to_allowed_values(clean_rows, schemas):
    result = run(clean_rows, schemas, "invoices", ["value_drift"])
    defect = only_defect(result, "value_drift")
    [target] = defect["target_columns"]
    allowed = next(c for c in schemas["invoices"]["columns"] if c["name"] == target)[
        "allowed_values"
    ]
    variants = defect["details"]["variants"]
    messy = messy_values(result, defect["source_column"])
    clean = clean_values(clean_rows, "invoices", target)
    changed = [(m, c) for m, c in zip(messy, clean, strict=True) if m != c]
    assert len(changed) == defect["affected_rows"]
    assert all(variants[m] == c for m, c in changed)
    assert not set(variants) & set(allowed)


def test_merge_columns_split_back_into_both_targets(clean_rows, schemas):
    result = run(clean_rows, schemas, "subscriptions", ["merge_columns"])
    defect = only_defect(result, "merged_column")
    first, second = defect["target_columns"]
    assert (first, second) in MERGE_PAIRS["subscriptions"]
    parts = {
        m["target_column"]: m["merged"]
        for m in result.answer_key["mappings"]
        if m["source_column"] == defect["source_column"]
    }
    assert parts[first]["part"] == 1
    assert parts[second]["part"] == 2
    separator = defect["details"]["separator"]
    for value, row in zip(
        messy_values(result, defect["source_column"]), clean_rows["subscriptions"], strict=True
    ):
        assert value.split(separator) == [row[first], row[second]]


def test_extra_columns_are_unmapped(clean_rows, schemas):
    result = run(clean_rows, schemas, "customers", ["extra_columns"])
    extras = result.answer_key["unmapped_source_columns"]
    assert 1 <= len(extras) <= 3
    assert set(extras) <= set(EXTRA_COLUMNS)
    assert [c for c in result.header if c not in extras] == column_names(schemas["customers"])
    assert result.answer_key["defects"] == []


def test_drop_required_removes_a_required_non_key_column(clean_rows, schemas):
    result = run(clean_rows, schemas, "invoices", ["drop_required"])
    [missing] = result.answer_key["missing_target_columns"]
    spec = next(c for c in schemas["invoices"]["columns"] if c["name"] == missing)
    assert spec["required"]
    assert missing != key_column(schemas["invoices"])
    assert missing not in result.header
    defect = only_defect(result, "missing_required_column")
    assert defect["source_column"] is None
    assert defect["target_columns"] == [missing]
    assert defect["affected_rows"] == len(clean_rows["invoices"])


# --- Combinations, errors and the full case set --------------------------------------------


def test_combined_corruptions_keep_defect_columns_distinct(clean_rows, schemas):
    corruptions = ["null_variants", "type_noise", "value_drift", "mixed_date_formats"]
    result = run(clean_rows, schemas, "subscriptions", corruptions)
    targets = [t for d in result.answer_key["defects"] for t in d["target_columns"]]
    assert len(targets) == len(set(targets)) == 4


def test_rejects_unknown_and_repeated_corruptions(clean_rows, schemas):
    with pytest.raises(ValueError, match="Unknown corruptions"):
        run(clean_rows, schemas, "customers", ["shuffle_everything"])
    with pytest.raises(ValueError, match="Duplicate"):
        run(clean_rows, schemas, "customers", ["duplicates", "duplicates"])


def test_infeasible_corruption_raises(clean_rows, schemas):
    no_nulls = [{**row, "country": "US"} for row in clean_rows["customers"]]
    with pytest.raises(InfeasibleCorruptionError):
        corrupt_table(no_nulls, schemas["customers"], 1, ["null_variants"])


def test_plan_covers_every_corruption_per_table_with_one_to_four_each():
    plan = plan_cases(seed=42)
    assert len(plan) == 30
    for table in TABLES:
        cases = [corruptions for _, t, _, corruptions in plan if t == table]
        assert len(cases) == 10
        assert {c for corruptions in cases for c in corruptions} == set(CORRUPTIONS)
        assert all(1 <= len(c) <= 4 and len(set(c)) == len(c) for c in cases)


def test_build_cases_is_deterministic_and_complete(tmp_path: Path):
    write_tables(generate(seed=5, customers=200), tmp_path / "clean")
    first = build_cases(tmp_path / "clean", tmp_path / "a", seed=8, rows=150)
    build_cases(tmp_path / "clean", tmp_path / "b", seed=8, rows=150)

    assert len(first) == 30
    for key in first:
        case_a, case_b = tmp_path / "a" / key["case_id"], tmp_path / "b" / key["case_id"]
        for name in ("messy.csv", "clean.csv", "answer_key.json"):
            assert (case_a / name).read_bytes() == (case_b / name).read_bytes()
        stored = json.loads((case_a / "answer_key.json").read_text())
        assert stored == key
        assert (case_a / stored["clean_file"]).is_file()
        assert 1 <= len(stored["corruptions"]) <= 4
