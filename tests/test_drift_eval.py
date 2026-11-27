"""The drift eval's generated cases raise exactly the findings their answer keys expect."""

from __future__ import annotations

import pytest

from agent.contracts import baseline_profile
from agent.drift import detect
from api.models import MappingContract
from datagen.schemas import TABLES, load_schema
from evals.drift import score, table_cases
from evals.oracle import oracle_sql
from tools.profiling import profile_file
from tools.warehouse import Warehouse, load_file


@pytest.mark.parametrize("table", TABLES)
def test_every_generated_case_raises_its_expected_findings(table, clean_rows, tmp_path, write_rows):
    schema = load_schema(table)
    specs = {c["name"]: c for c in schema["columns"]}
    base, cases = table_cases(schema, clean_rows[table], rows=60, seed=3)
    wh = Warehouse.for_run("base", tmp_path / "data")
    info = load_file(wh, write_rows(base, "base.csv"))
    mappings = [
        {
            "source_column": c,
            "target_column": c,
            "transform_sql": oracle_sql({"source_column": c, "target_column": c}, specs[c], {}),
        }
        for c in base[0]
    ]
    contract = MappingContract(
        baseline_profile=baseline_profile(wh, profile_file(wh, info.file_id), schema, mappings),
        mappings=mappings,
    )

    for name, (rows, expected) in cases.items():
        batch = Warehouse.for_run(f"case-{name}", tmp_path / "data")
        file = load_file(batch, write_rows(rows, f"{name}.csv"))
        found = detect(contract, profile_file(batch, file.file_id), batch, schema, [len(base)])
        assert [[f.check, f.source_column] for f in found] == expected["findings"], name
        assert any(f.action == "block" for f in found) == expected["blocked"], name


def test_the_scorecard_counts_false_alarms_and_misses():
    results = [
        {"kind": "unchanged", "expected": [], "found": [["null_rate_jump", "seats"]],
         "status": "done", "error": None, "blocked_expected": False, "model_calls": 0,
         "proposals": 0, "proposals_approved": 0},
        {"kind": "unchanged", "expected": [], "found": [], "status": "done", "error": None,
         "blocked_expected": False, "model_calls": 0, "proposals": 0, "proposals_approved": 0},
        {"kind": "column_missing_required", "expected": [["column_missing", "customer_id"]],
         "found": [["column_missing", "customer_id"]], "status": "failed",
         "error": "Load blocked: ...", "blocked_expected": True, "model_calls": 0,
         "proposals": 0, "proposals_approved": 0},
        {"kind": "format_change", "expected": [["format_change", "started_at"]], "found": [],
         "status": "done", "error": None, "blocked_expected": False, "model_calls": 1,
         "proposals": 2, "proposals_approved": 1},
    ]  # fmt: skip
    summary = score(results)
    assert summary["false_alarm_rate"] == 0.5
    assert summary["unchanged_loaded_without_model"] == 2
    assert (summary["detection_recall"], summary["detection_precision"]) == (0.5, 0.5)
    assert summary["per_check"]["format_change"] == {"tp": 0, "fp": 0, "fn": 1}
    assert summary["correct_outcomes"] == 1.0
    assert summary["proposals_accepted"] == 0.5
