from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.llm import FakeLLM
from datagen.generate import generate, write_tables
from evals.corrupt import build_cases
from evals.oracle import oracle_responder
from evals.run_evals import run_evals, select_cases, smoke_failures
from evals.scoring import CaseScore, aggregate, normalize_cell, score_case

KEY = {
    "case_id": "customers_01",
    "table": "customers",
    "names_changed": True,
    "corruptions": ["rename_columns", "merge_columns", "extra_columns", "drop_required"],
    "mappings": [
        {"source_column": "Cust #", "target_column": "customer_id"},
        {"source_column": "Plan / Active", "target_column": "plan"},
        {"source_column": "Plan / Active", "target_column": "is_active"},
        {"source_column": "Joined", "target_column": "signup_date"},
    ],
    "unmapped_source_columns": ["sales_rep"],
    "missing_target_columns": ["company_name"],
    "defects": [
        {
            "type": "merged_column",
            "source_column": "Plan / Active",
            "target_columns": ["plan", "is_active"],
        },
        {"type": "missing_required_column", "source_column": None, "target_columns": ["x"]},
        {"type": "duplicate_rows", "source_column": "Cust #", "target_columns": ["customer_id"]},
    ],
}


def mapping(source, target, confidence=0.9):
    review = "low" if confidence is None or confidence < 0.8 else None
    return SimpleNamespace(
        source_column=source, target_column=target, confidence=confidence, review_reason=review
    )


def defect(defect_type, column=None, target=None):
    return SimpleNamespace(defect_type=defect_type, column_name=column, target_column=target)


def fake_run(mappings, defects):
    return SimpleNamespace(
        status="awaiting_review",
        error=None,
        mappings=mappings,
        defects=defects,
        build_report=None,
        model_calls=3,
        tool_calls=4,
        prompt_tokens=100,
        completion_tokens=20,
        duration_seconds=1.5,
    )


def test_scores_mappings_defects_escalations_and_bad_auto_approvals():
    run = fake_run(
        [
            mapping("Cust #", "customer_id", 0.99),
            mapping("Plan / Active", "plan", 0.9),  # merged column only half mapped
            mapping("Joined", "signup_date", 0.5),  # correct but escalated
            mapping("sales_rep", "company_name", 0.85),  # wrong and auto-approved
            mapping(None, "country", None),  # declared missing
        ],
        [
            defect("merged_column", column="Plan / Active"),
            defect("duplicate_rows"),  # table-level: column not needed
            defect("value_drift", column="Joined"),  # not injected
        ],
    )
    score = score_case(KEY, run, threshold=0.8)
    assert (score.source_columns, score.mapping_correct) == (4, 2)
    assert score.wrong_mappings == [
        {"source": "Plan / Active", "predicted": ["plan"], "truth": ["is_active", "plan"]},
        {
            "source": "sales_rep",
            "predicted": ["company_name"],
            "truth": [],
            "auto_approved_confidence": 0.85,
        },
    ]
    assert score.bad_auto_approvals == 1
    assert (score.escalated, score.mappings_total) == (2, 5)
    assert (score.defects_recalled, score.defects_injected) == (2, 3)
    assert score.missed_defects == ["missing_required_column@['x']"]
    assert (score.defects_flagged_correct, score.defects_flagged) == (2, 3)
    assert score.spurious_defects == ["value_drift@Joined"]


def test_missing_column_defect_matches_on_target():
    run = fake_run([], [defect("missing_required_column", target="x")])
    assert score_case(KEY, run, 0.8).defects_recalled == 1


def test_aggregate_is_micro_averaged_and_split_by_naming():
    a = CaseScore("a", "t", True, [], "done", None, source_columns=8, mapping_correct=6)
    b = CaseScore("b", "t", False, [], "failed", "x", source_columns=2, mapping_correct=2)
    a.cells_total, a.cells_correct, b.cells_total = 100, 90, 100
    a.build_passed = True
    summary = aggregate([a, b])
    assert summary["mapping_accuracy"] == 0.8
    assert summary["mapping_accuracy_names_changed"] == 0.75
    assert summary["mapping_accuracy_names_unchanged"] == 1.0
    assert summary["failed_runs"] == 1
    assert summary["defect_recall"] is None
    assert summary["cell_accuracy"] == 0.45
    assert summary["build_success"] == 0.5
    assert summary["consistency"] is None  # needs repeated runs


def test_consistency_counts_cases_that_pass_every_repeat():
    def run(case, passed, repeat):
        s = CaseScore(case, "t", True, [], "done", None, repeat=repeat, source_columns=4)
        s.mapping_correct = 4 if passed else 3
        s.build_passed = True
        return s

    scores = [run("a", True, 1), run("a", True, 2), run("b", True, 1), run("b", False, 2)]
    summary = aggregate(scores)
    assert (summary["cases"], summary["runs"], summary["consistency"]) == (2, 4, 0.5)


@pytest.mark.parametrize(
    ("loaded", "clean", "column_type"),
    [
        (date(2024, 3, 1), "2024-03-01", "date"),
        (Decimal("1200.5"), "1200.50", "decimal(10,2)"),
        (7, "7", "integer"),
        (True, "true", "boolean"),
        (None, "", "varchar"),
        ("CUS-000001", "CUS-000001", "varchar"),
    ],
)
def test_loaded_values_compare_equal_to_clean_text(loaded, clean, column_type):
    assert normalize_cell(loaded, column_type) == normalize_cell(clean, column_type)


def test_cell_normalization_keeps_real_differences():
    decimal = "decimal(10,2)"
    assert normalize_cell(Decimal("1200.00"), decimal) != normalize_cell("120.00", decimal)
    assert normalize_cell(False, "boolean") != normalize_cell("true", "boolean")
    assert normalize_cell(None, "date") != normalize_cell("2024-01-01", "date")


def test_select_cases_interleaves_tables(tmp_path: Path):
    for name in ["customers_01", "customers_02", "invoices_01", "subscriptions_01", "junk"]:
        (tmp_path / name).mkdir()
        if name != "junk":
            (tmp_path / name / "answer_key.json").write_text("{}")
    picked = [p.name for p in select_cases(tmp_path, None, 3)]
    assert picked == ["customers_01", "invoices_01", "subscriptions_01"]
    assert [p.name for p in select_cases(tmp_path, ["customers_02"], None)] == ["customers_02"]


def test_answer_key_run_maps_builds_and_writes_a_scorecard(tmp_path: Path, settings):
    """The eval-smoke path: real runner, review, dbt build, and scoring, with FakeLLM answering
    from the answer keys. Repeated runs must agree."""
    write_tables(generate(seed=5, customers=150), tmp_path / "clean")
    build_cases(tmp_path / "clean", tmp_path / "cases", seed=3, rows=120)
    cases = select_cases(tmp_path / "cases", None, 3)
    summary, scores, markdown = run_evals(
        cases,
        settings,
        lambda key: FakeLLM(oracle_responder(key), model="fake-oracle"),
        tmp_path / "results",
        repeats=2,
    )
    assert smoke_failures(summary) == [], summary
    assert summary["consistency"] == 1.0  # deterministic answers pass on every repeat
    assert [(s.case_id, s.repeat) for s in scores] == [(c.name, r) for c in cases for r in (1, 2)]
    assert all(s.rows_loaded + s.rows_rejected >= 120 for s in scores)
    assert summary["cell_accuracy"] > 0
    text = markdown.read_text()
    assert "| Mapping accuracy | 100.0% |" in text
    assert "| Build success | 100.0% |" in text
    stored = json.loads(markdown.with_suffix(".json").read_text())
    assert stored["summary"] == summary
    assert stored["meta"]["model"] == "fake-oracle"
