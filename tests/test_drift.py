"""Drift checks on a second batch of subscriptions, against a contract built from the first."""

from __future__ import annotations

from datetime import date

import pytest

from agent.contracts import baseline_profile
from agent.drift import detect
from api.models import MappingContract
from datagen.schemas import load_schema
from evals.oracle import oracle_sql
from tools.profiling import profile_file
from tools.warehouse import Warehouse, load_file

SCHEMA = load_schema("subscriptions")
SPECS = {c["name"]: c for c in SCHEMA["columns"]}


@pytest.fixture
def rows(clean_rows) -> list[dict[str, str]]:
    return [dict(r) for r in clean_rows["subscriptions"]]


@pytest.fixture
def contract(tmp_path, rows, write_rows) -> MappingContract:
    """A contract saved from the first 150 rows, with the oracle's transforms."""
    wh = Warehouse.for_run("baseline", tmp_path / "data")
    info = load_file(wh, write_rows(rows[:150], "baseline.csv"))
    mappings = [
        {
            "source_column": c,
            "target_column": c,
            "transform_sql": oracle_sql({"source_column": c, "target_column": c}, SPECS[c], {}),
        }
        for c in rows[0]
    ]
    return MappingContract(
        customer_name="Acme Cloud",
        source_name="billing-export",
        version=1,
        target_schema="subscriptions",
        fingerprint="",
        baseline_profile=baseline_profile(wh, profile_file(wh, info.file_id), SCHEMA, mappings),
        mappings=mappings,
        created_from_run_id="baseline",
    )


@pytest.fixture
def findings(tmp_path, contract, write_rows):
    def check(batch: list[dict[str, str]]) -> list[tuple[str, str | None, str]]:
        wh = Warehouse.for_run("batch", tmp_path / "data")
        info = load_file(wh, write_rows(batch, "batch.csv"))
        found = detect(contract, profile_file(wh, info.file_id), wh, SCHEMA, [150])
        return [(f.check, f.source_column, f.action) for f in found]

    return check


def _renamed(rows, old, new):
    return [{(new if k == old else k): v for k, v in r.items()} for r in rows]


def _dropped(rows, column):
    return [{k: v for k, v in r.items() if k != column} for r in rows]


def _with_dotted_dates(rows):
    def dotted(value: str) -> str:
        return date.fromisoformat(value).strftime("%d.%m.%Y")

    return [{**r, "started_at": dotted(r["started_at"])} for r in rows]


def _new_plan(rows):
    return [{**r, "plan": "Pro Plus"} if r["plan"] == "pro" and i % 2 else r
            for i, r in enumerate(rows)]  # fmt: skip


def _blank_seats(rows):
    return [{**r, "seats": ""} if i % 5 < 2 else r for i, r in enumerate(rows)]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda rows: rows, []),
        (
            lambda rows: [{**r, "notes": f"note {i}"} for i, r in enumerate(rows)],
            [("column_added", "notes", "propose")],
        ),
        (lambda rows: _dropped(rows, "customer_id"), [("column_missing", "customer_id", "block")]),
        (lambda rows: _dropped(rows, "canceled_at"), [("column_missing", "canceled_at", "warn")]),
        (
            lambda rows: _renamed(rows, "seats", "seat_count"),
            [("column_renamed", "seat_count", "propose")],
        ),
        (_with_dotted_dates, [("format_change", "started_at", "propose")]),
        (_new_plan, [("new_category", "plan", "propose")]),
        (_blank_seats, [("null_rate_jump", "seats", "warn")]),
        (lambda rows: rows[:30], [("volume_anomaly", None, "warn")]),
    ],
    ids=[
        "unchanged",
        "column_added",
        "required_column_missing",
        "optional_column_missing",
        "column_renamed",
        "format_change",
        "new_category",
        "null_rate_jump",
        "volume_anomaly",
    ],
)
def test_each_drift_type_is_detected(rows, findings, change, expected):
    assert findings(change(rows[150:])) == expected


def test_a_reordered_unchanged_batch_raises_no_alarm(rows, findings):
    batch = rows[150:]
    reordered = [{k: r[k] for k in reversed(list(r))} for r in reversed(batch)]
    assert findings(reordered) == []
