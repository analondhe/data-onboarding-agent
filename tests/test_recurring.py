"""Recurring loads end to end: a saved source, then later files checked, built, and upserted."""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from agent import contracts
from agent.llm import FakeLLM, FakeReply
from agent.review import Decision, apply_review, finish_review
from agent.runner import advance, create_load, onboard
from agent.tracing import Tracer
from api.db import session_factory
from api.models import MappingStatus, RunStatus
from datagen.schemas import load_schema
from evals.oracle import answer_key_reviewer, oracle_responder, oracle_sql

SPECS = {c["name"]: c for c in load_schema("subscriptions")["columns"]}


def no_model(messages):
    pytest.fail("the model was called for a load that needed none")


@pytest.fixture
def rows(clean_rows) -> list[dict[str, str]]:
    return [dict(r) for r in clean_rows["subscriptions"]]


@pytest.fixture
def source(settings, rows, write_rows):
    """An onboarded first batch (150 rows) saved as the source 'billing-export'."""
    key = {
        "table": "subscriptions",
        "mappings": [{"source_column": c, "target_column": c} for c in rows[0]],
        "missing_target_columns": [],
        "defects": [],
    }
    session = session_factory(settings.database_url)()
    run = onboard(
        session, settings, FakeLLM(oracle_responder(key)), Tracer(None),
        customer_name="Acme Cloud", schema_name="subscriptions",
        paths=[write_rows(rows[:150], "first.csv")], review=answer_key_reviewer(key),
    )  # fmt: skip
    assert run.status == RunStatus.DONE, run.error
    contract = contracts.save_contract(session, settings, run, "billing-export")
    assert run.build_report["load"] == {
        "table": "subscriptions", "inserted": 150, "updated": 0, "total_rows": 150,
    }  # fmt: skip
    yield session, contract
    session.close()


def load(settings, session, contract, path, llm=None):
    run = create_load(session, settings, contract, path)
    return advance(session, settings, llm or FakeLLM(no_model), Tracer(None), run)


def table(settings, contract) -> list[tuple]:
    con = duckdb.connect(str(contracts.source_table_path(settings, contract)), read_only=True)
    try:
        return con.execute(
            "SELECT run_id, count(*) FROM subscriptions GROUP BY 1 ORDER BY 1"
        ).fetchall()
    finally:
        con.close()


def test_an_unchanged_file_loads_with_no_model_call_and_upserts_on_the_key(
    settings, source, rows, write_rows
):
    session, contract = source
    second = load(settings, session, contract, write_rows(rows[150:], "second.csv"))
    assert (second.status, second.error, second.drift) == (RunStatus.DONE, None, [])
    assert second.model_calls == 0
    assert all(m.status == MappingStatus.APPROVED for m in second.mappings)
    assert second.build_report["load"] == {
        "table": "subscriptions", "inserted": 150, "updated": 0, "total_rows": 300,
    }  # fmt: skip

    again = load(settings, session, contract, write_rows(rows[:150], "first-again.csv"))
    assert again.build_report["load"]["inserted"] == 0
    assert again.build_report["load"]["updated"] == 150
    assert dict(table(settings, contract)) == {second.id: 150, again.id: 150}
    assert len(contracts.versions(session, contract)) == 1  # nothing changed


def test_a_format_change_waits_for_review_then_saves_a_new_version(
    settings, source, rows, write_rows
):
    session, contract = source
    batch = [
        {**r, "started_at": date.fromisoformat(r["started_at"]).strftime("%d.%m.%Y")}
        for r in rows[150:]
    ]
    new_sql = "try_strptime(trim(\"started_at\"), ['%Y-%m-%d', '%d.%m.%Y'])::DATE"
    proposal = {
        "mappings": [
            {"source_column": "started_at", "target_column": "started_at",
             "transform_sql": new_sql, "confidence": 0.97, "reason": "dotted dates"},
        ],
    }  # fmt: skip
    llm = FakeLLM([FakeReply(tool_calls=[("submit_mappings", proposal)])])
    run = load(settings, session, contract, write_rows(batch, "dotted.csv"), llm)

    assert (run.status, run.model_calls) == (RunStatus.AWAITING_REVIEW, 1)
    assert [d["check"] for d in run.drift] == ["format_change"]
    [pending] = [m for m in run.mappings if m.status == MappingStatus.PROPOSED]
    assert pending.target_column == "started_at"
    assert pending.review_reason.startswith("Values in 'started_at' changed format")

    apply_review(session, run, [Decision(mapping_id=pending.id, action="approve")], "test")
    finish_review(session, run)
    run = advance(session, settings, FakeLLM(no_model), Tracer(None), run)
    assert (run.status, run.error) == (RunStatus.DONE, None)
    assert run.build_report["load"]["inserted"] == 150

    latest = contracts.latest_version(session, "Acme Cloud", "billing-export")
    assert (latest.version, latest.created_from_run_id) == (2, run.id)
    saved = {m["target_column"]: m["transform_sql"] for m in latest.mappings}
    assert "%d.%m.%Y" in saved["started_at"]


def test_unknown_categories_are_quarantined_when_the_saved_mapping_is_kept(
    settings, source, rows, write_rows
):
    session, contract = source
    batch = [{**r, "plan": "Pro Plus"} if r["plan"] == "pro" else r for r in rows[150:]]
    unknown = sum(r["plan"] == "Pro Plus" for r in batch)
    saved_sql = oracle_sql({"source_column": "plan", "target_column": "plan"}, SPECS["plan"], {})
    proposal = {
        "mappings": [
            {"source_column": "plan", "target_column": "plan",
             "transform_sql": "CASE lower(trim(plan)) WHEN 'pro plus' THEN 'pro' "
                              "ELSE lower(trim(plan)) END",
             "confidence": 0.9, "reason": "Pro Plus is the pro tier"},
        ],
    }  # fmt: skip
    llm = FakeLLM([FakeReply(tool_calls=[("submit_mappings", proposal)])])
    run = load(settings, session, contract, write_rows(batch, "plans.csv"), llm)
    assert [d["check"] for d in run.drift] == ["new_category"]
    [pending] = [m for m in run.mappings if m.status == MappingStatus.PROPOSED]

    keep_old = Decision(mapping_id=pending.id, action="edit", transform_sql=saved_sql)
    apply_review(session, run, [keep_old], "test")
    finish_review(session, run)
    run = advance(session, settings, FakeLLM(no_model), Tracer(None), run)

    assert (run.status, run.error) == (RunStatus.DONE, None)
    [accounting] = run.build_report["row_accounting"]
    assert accounting["reject_reasons"] == {"new category plan": unknown}
    assert run.build_report["load"]["inserted"] == 150 - unknown


def test_a_missing_required_column_blocks_the_load(settings, source, rows, write_rows):
    session, contract = source
    batch = [{k: v for k, v in r.items() if k != "customer_id"} for r in rows[150:]]
    run = load(settings, session, contract, write_rows(batch, "no-customer.csv"))
    assert run.status == RunStatus.FAILED
    assert run.error.startswith("Load blocked: Column 'customer_id' is missing.")
    assert run.mappings == []
    assert table(settings, contract) == [(contract.created_from_run_id, 150)]
