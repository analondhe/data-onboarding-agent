from __future__ import annotations

import pytest

from agent.defects import DefectType
from agent.mapping import (
    MappingSubmission,
    ProposedDefect,
    route_for_review,
    validate_submission,
)
from datagen.schemas import load_schema

SCHEMA = load_schema("subscriptions")
SOURCES = ["Sub #", "Cust #", "plan_status", "Start Date", "End Date", "MRR", "Seats", "notes"]


def mapping(source: str, target: str, sql: str | None = None, confidence: float = 0.9) -> dict:
    return {
        "source_column": source,
        "target_column": target,
        "transform_sql": sql or f'trim("{source}")',
        "confidence": confidence,
        "reason": "test",
    }


def valid_mappings() -> list[dict]:
    return [
        mapping("Sub #", "subscription_id"),
        mapping("Cust #", "customer_id"),
        mapping("plan_status", "plan", "split_part(\"plan_status\", '|', 1)"),
        mapping("plan_status", "status", "split_part(\"plan_status\", '|', 2)", confidence=0.6),
        mapping("Start Date", "started_at", 'try_cast("Start Date" AS DATE)'),
        mapping("MRR", "monthly_amount_usd"),
        mapping("Seats", "seats"),
    ]


def check(mappings: list[dict], **extra) -> tuple[MappingSubmission, list[str]]:
    submission = MappingSubmission.model_validate({"mappings": mappings, **extra})
    return validate_submission(submission, SCHEMA, SOURCES)


def test_valid_submission_is_accepted_with_normalized_sql():
    normalized, errors = check(valid_mappings())
    assert errors == []
    started = next(m for m in normalized.mappings if m.target_column == "started_at")
    assert started.transform_sql == 'TRY_CAST("Start Date" AS DATE)'


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda ms: [*ms, mapping("notes", "notes")], "'notes': unknown target column"),
        (lambda ms: [*ms, mapping("notes", "seats")], "target 'seats' is already mapped from"),
        (lambda ms: [*ms, mapping("Nope", "canceled_at")], "unknown source column"),
        (
            lambda ms: [*ms, mapping("End Date", "canceled_at", "DROP TABLE raw.messy")],
            "transform_sql refused",
        ),
        (
            lambda ms: [*ms, mapping("End Date", "canceled_at", 'trim("notes")')],
            "transform_sql must read its source column 'End Date'",
        ),
        (lambda ms: ms[1:], "required target column 'subscription_id' has no mapping"),
    ],
)
def test_problems_are_reported_for_the_model_to_fix(change, message):
    _, errors = check(change(valid_mappings()))
    assert any(message in e for e in errors), errors


def test_missing_required_target_can_be_declared_instead_of_mapped():
    without_seats = [m for m in valid_mappings() if m["target_column"] != "seats"]
    _, errors = check(
        without_seats, missing_required=[{"target_column": "seats", "reason": "not in file"}]
    )
    assert errors == []
    _, errors = check(
        valid_mappings(), missing_required=[{"target_column": "seats", "reason": "dup"}]
    )
    assert errors == ["missing_required: 'seats' is also mapped; remove one of them"]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ({"source_column": "MRR"}, ("MRR", None)),
        ({"target_column": "MRR"}, ("MRR", None)),  # a source name given as the target
        ({"source_column": "seats"}, (None, "seats")),  # a target name given as the source
        ({"source_column": "Nope", "target_column": "nope"}, (None, None)),
    ],
)
def test_misplaced_defect_columns_are_normalized_not_rejected(given, expected):
    defect = {"defect_type": "type_noise", "description": "x", **given}
    normalized, errors = check(valid_mappings(), defects=[defect])
    assert errors == []
    [d] = normalized.defects
    assert (d.source_column, d.target_column) == expected


def test_unknown_defect_type_fails_argument_validation():
    with pytest.raises(ValueError, match="defect_type"):
        MappingSubmission.model_validate(
            {"mappings": [], "defects": [{"defect_type": "weird", "description": "x"}]}
        )


def test_routing_flags_low_confidence_and_derives_structural_defects():
    without_seats = [m for m in valid_mappings() if m["target_column"] != "seats"]
    submission, _ = check(
        without_seats, missing_required=[{"target_column": "seats", "reason": "not in file"}]
    )
    reviewed, defects = route_for_review(submission, threshold=0.8)
    flagged = {m.target_column: m.review_reason for m in reviewed if m.needs_review}
    assert flagged == {"status": "confidence 0.60 is below 0.80"}
    assert [(d.defect_type, d.source_column, d.target_column) for d in defects] == [
        (DefectType.MERGED_COLUMN, "plan_status", None),
        (DefectType.MISSING_REQUIRED_COLUMN, None, "seats"),
    ]


def test_routing_does_not_duplicate_defects_the_model_reported():
    submission, _ = check(
        valid_mappings(),
        defects=[
            {"defect_type": "merged_column", "source_column": "plan_status", "description": "m"}
        ],
    )
    _, defects = route_for_review(submission, threshold=0.8)
    assert defects == [
        ProposedDefect(
            defect_type=DefectType.MERGED_COLUMN, source_column="plan_status", description="m"
        )
    ]
