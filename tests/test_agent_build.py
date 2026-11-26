from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import select

from agent.llm import FakeLLM, FakeReply
from agent.review import Decision, apply_review, approve_all
from agent.runner import build_run, onboard
from agent.tracing import Tracer
from api.db import session_factory
from api.models import MappingStatus, ReviewEvent, RunStatus
from datagen.schemas import load_schema
from evals.corrupt import write_case
from evals.oracle import answer_key_reviewer, oracle_responder, oracle_sql
from tools.warehouse import Warehouse


def make_case(tmp_path: Path, clean_rows, schemas, table: str, corruptions, seed: int = 4):
    key = write_case(tmp_path / "case", "c", clean_rows[table], schemas[table], seed, corruptions)
    return tmp_path / "case" / "messy.csv", key


def run_onboard(settings, llm, path: Path, table: str, review=None):
    session = session_factory(settings.database_url)()
    run = onboard(
        session,
        settings,
        llm,
        Tracer(None),
        customer_name="Acme Cloud",
        schema_name=table,
        paths=[path],
        review=review,
        sleep=lambda s: None,
    )
    return run, session


def drift_target(key) -> str:
    [drift] = [d for d in key["defects"] if d["type"] == "value_drift"]
    return drift["target_columns"][0]


def test_reviewed_run_builds_clean_table_and_accounts_for_every_row(
    settings, tmp_path, clean_rows, schemas
):
    corruptions = ["value_drift", "type_noise", "duplicates", "merge_columns"]
    path, key = make_case(tmp_path, clean_rows, schemas, "subscriptions", corruptions)
    run, _ = run_onboard(
        settings, FakeLLM(oracle_responder(key)), path, "subscriptions", answer_key_reviewer(key)
    )
    assert (run.status, run.error) == (RunStatus.DONE, None)
    report = run.build_report
    assert report["status"] == "passed" and report["repairs"] == 0
    [accounting] = report["row_accounting"]
    assert accounting["balanced"]
    assert accounting["rows_in"] == key["messy_rows"]
    dupes = next(d for d in key["defects"] if d["type"] == "duplicate_rows")["affected_rows"]
    duplicate_rejects = sum(
        n for reason, n in accounting["reject_reasons"].items() if reason.startswith("duplicate")
    )
    assert duplicate_rejects == dupes
    assert report["dbt"]["tests_failed"] == 0


def repairing_responder(key, schemas, table: str):
    """Maps like the oracle but leaves synonyms in the drifted column, then repairs it."""
    oracle = oracle_responder(key)
    target = drift_target(key)
    spec = next(c for c in schemas[table]["columns"] if c["name"] == target)
    source = next(m for m in key["mappings"] if m["target_column"] == target)

    def respond(messages):
        step = oracle(messages)
        if messages[0]["content"].startswith("You repair a failing dbt build"):
            mappings = json.loads(
                re.search(r"Mappings:\n(.*?)\n\nFailures", messages[1]["content"], re.S)[1]
            )
            mapping_id = next(m["mapping_id"] for m in mappings if m["target_column"] == target)
            fixed = oracle_sql(source, spec, drift_variants(key))
            update = {"mapping_id": mapping_id, "transform_sql": fixed, "reason": "map synonyms"}
            return FakeReply(
                tool_calls=[("get_dbt_errors", {}), ("update_transform", update), ("run_dbt", {})]
            )
        if step.tool_calls and step.tool_calls[0][0] == "submit_mappings":
            submission = step.tool_calls[0][1]
            for m in submission["mappings"]:
                if m["target_column"] == target:
                    m["transform_sql"] = f'lower(trim("{m["source_column"]}"))'
        return step

    return respond


def drift_variants(key) -> dict[str, str]:
    return next(d for d in key["defects"] if d["type"] == "value_drift")["details"]["variants"]


def test_failing_build_is_repaired_by_the_agent_and_recorded(
    settings, tmp_path, clean_rows, schemas
):
    path, key = make_case(tmp_path, clean_rows, schemas, "subscriptions", ["value_drift"])
    assert any(v.strip().lower() != c for v, c in drift_variants(key).items())  # has synonyms
    llm = FakeLLM(repairing_responder(key, schemas, "subscriptions"))
    run, session = run_onboard(settings, llm, path, "subscriptions", answer_key_reviewer(key))

    assert (run.status, run.error) == (RunStatus.DONE, None)
    assert run.build_report["repairs"] == 1
    [event] = session.scalars(
        select(ReviewEvent).where(
            ReviewEvent.run_id == run.id, ReviewEvent.action == "agent_repair"
        )
    ).all()
    assert event.before["transform_sql"].startswith("LOWER(TRIM(")
    assert event.after["transform_sql"].startswith("CASE WHEN")
    assert event.after["reason"] == "map synonyms"
    repaired = next(m for m in run.mappings if m.id == event.item_id)
    assert repaired.transform_sql == event.after["transform_sql"]


def test_repairs_stop_at_the_limit_and_fail_with_the_last_error(
    settings, tmp_path, clean_rows, schemas
):
    path, key = make_case(tmp_path, clean_rows, schemas, "subscriptions", ["value_drift"])
    target = drift_target(key)
    oracle = oracle_responder(key)

    def stubborn(messages):
        if messages[0]["content"].startswith("You repair a failing dbt build"):
            return FakeReply(tool_calls=[("run_dbt", {})])
        step = oracle(messages)
        if step.tool_calls and step.tool_calls[0][0] == "submit_mappings":
            for m in step.tool_calls[0][1]["mappings"]:
                if m["target_column"] == target:
                    m["transform_sql"] = f'trim("{m["source_column"]}")'
        return step

    run, _ = run_onboard(
        settings, FakeLLM(stubborn), path, "subscriptions", answer_key_reviewer(key)
    )
    assert run.status == RunStatus.FAILED
    assert run.build_report["repairs"] == settings.build_max_repairs
    assert run.error.startswith(
        f"dbt build failed: test accepted_values_stg_subscriptions_{target}"
    )


def test_missing_required_column_quarantines_every_row(settings, tmp_path, clean_rows, schemas):
    path, key = make_case(tmp_path, clean_rows, schemas, "customers", ["drop_required"])
    [missing] = key["missing_target_columns"]
    run, session = run_onboard(
        settings, FakeLLM(oracle_responder(key)), path, "customers", approve_all
    )

    assert run.status == RunStatus.DONE
    [accounting] = run.build_report["row_accounting"]
    assert (accounting["loaded"], accounting["rejected"]) == (0, key["messy_rows"])
    assert accounting["reject_reasons"] == {f"missing required {missing}": key["messy_rows"]}
    approvals = session.scalars(select(ReviewEvent).where(ReviewEvent.action == "approve")).all()
    assert [(e.after["target_column"], e.after["actor"]) for e in approvals] == [(missing, "cli")]


def test_run_reviewed_later_builds_only_after_every_decision(
    settings, tmp_path, clean_rows, schemas
):
    path, key = make_case(tmp_path, clean_rows, schemas, "invoices", ["rename_columns"])
    oracle = oracle_responder(key)

    def unsure(messages):
        step = oracle(messages)
        if step.tool_calls and step.tool_calls[0][0] == "submit_mappings":
            step.tool_calls[0][1]["mappings"][0]["confidence"] = 0.5
        return step

    run, session = run_onboard(settings, FakeLLM(unsure), path, "invoices")
    assert run.status == RunStatus.AWAITING_REVIEW
    [pending] = [m for m in run.mappings if m.status == MappingStatus.PROPOSED]

    build_run(session, settings, None, Tracer(None), run)
    assert (run.status, run.error) == (
        RunStatus.FAILED,
        "1 mappings still need review before the build",
    )

    apply_review(
        session, run, Warehouse.for_run(run.id, settings.data_dir),
        [Decision(mapping_id=pending.id, action="approve")], actor="tester",
    )  # fmt: skip
    build_run(session, settings, None, Tracer(None), run)
    assert (run.status, run.error) == (RunStatus.DONE, None)


def test_edit_decisions_are_validated(settings, tmp_path, clean_rows, schemas):
    path, key = make_case(tmp_path, clean_rows, schemas, "invoices", ["rename_columns"])
    run, session = run_onboard(settings, FakeLLM(oracle_responder(key)), path, "invoices")
    wh = Warehouse.for_run(run.id, settings.data_dir)
    mapping = run.mappings[0]
    with pytest.raises(Exception, match="Expected a single expression"):
        apply_review(
            session, run, wh,
            [Decision(mapping_id=mapping.id, action="edit", transform_sql="DROP TABLE raw.messy")],
            actor="tester",
        )  # fmt: skip
    column = mapping.source_column
    apply_review(
        session, run, wh,
        [Decision(mapping_id=mapping.id, action="edit", transform_sql=f'lower("{column}")')],
        actor="tester",
    )  # fmt: skip
    assert (mapping.status, mapping.transform_sql) == (MappingStatus.EDITED, f'LOWER("{column}")')


def test_schema_has_a_unique_key_for_deduplication():
    for table in ("customers", "subscriptions", "invoices"):
        assert any(c.get("unique") for c in load_schema(table)["columns"])
