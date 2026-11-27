from __future__ import annotations

import json
from pathlib import Path

import mlflow
import pytest

from agent.llm import FakeError, FakeLLM, FakeReply, FakeTimeout
from agent.runner import onboard
from agent.settings import Settings
from agent.tracing import Tracer
from api.db import session_factory
from api.models import MappingStatus, RunStatus
from evals.corrupt import write_case
from evals.oracle import oracle_responder
from evals.scoring import score_case

CORRUPTIONS = ["cryptic_names", "merge_columns", "drop_required", "null_variants"]


@pytest.fixture
def case(tmp_path: Path, clean_rows, schemas) -> tuple[Path, dict]:
    case_dir = tmp_path / "case"
    key = write_case(
        case_dir, "subs", clean_rows["subscriptions"], schemas["subscriptions"], 11, CORRUPTIONS
    )
    return case_dir / "messy.csv", key


def run_onboard(settings: Settings, llm, path: Path, tracer: Tracer | None = None):
    sessions = session_factory(settings.database_url)
    session = sessions()
    run = onboard(
        session,
        settings,
        llm,
        tracer or Tracer(None),
        customer_name="Acme Cloud",
        schema_name="subscriptions",
        paths=[path],
        sleep=lambda s: None,
    )
    return run, session


def test_file_to_reviewed_mappings_with_one_trace(settings, case):
    path, key = case
    tracer = Tracer(settings.mlflow_tracking_uri)
    run, _ = run_onboard(settings, FakeLLM(oracle_responder(key)), path, tracer)

    assert (run.status, run.error) == (RunStatus.AWAITING_REVIEW, None)
    assert (run.model_calls, run.tool_calls) == (2, 3)
    [source] = run.files
    header = path.read_text().splitlines()[0].split(",")
    assert [p.column_name for p in source.profiles] == header

    missing = [m for m in run.mappings if m.source_column is None]
    assert [(m.target_column, m.status) for m in missing] == [
        (key["missing_target_columns"][0], MappingStatus.PROPOSED)
    ]
    assert all(m.status == MappingStatus.APPROVED for m in run.mappings if m.source_column)
    assert sorted(d.defect_type for d in run.defects) == sorted(d["type"] for d in key["defects"])

    score = score_case(key, run, 0.8)
    assert score.mapping_correct == score.source_columns
    assert score.defects_recalled == score.defects_injected
    assert score.bad_auto_approvals == 0

    trace = mlflow.get_trace(run.trace_id)
    names = [span.name for span in trace.data.spans]
    assert names[0] == "run"
    assert names.count("llm") == 2
    assert {"phase:profile:messy.csv", "tool:get_column_profile", "tool:submit_mappings"} <= set(
        names
    )
    llm_span = next(s for s in trace.data.spans if s.name == "llm")
    assert llm_span.attributes["model"] == "fake-llm"
    assert llm_span.attributes["prompt_tokens"] > 0
    assert trace.info.tags["run_id"] == run.id


def test_model_timeouts_fail_the_run_with_a_reason(settings, case):
    path, _ = case
    run, _ = run_onboard(settings, FakeLLM([FakeTimeout()] * 4), path)
    assert run.status == RunStatus.FAILED
    assert run.error.startswith("Mapping messy.csv failed: The model did not respond after 4")
    assert run.mappings == []
    assert len(run.files[0].profiles) > 0  # profiling finished before the model was needed


def test_permanent_model_error_fails_without_retrying(settings, case):
    path, _ = case
    llm = FakeLLM([FakeError("AuthenticationError: invalid API key")])
    run, _ = run_onboard(settings, llm, path)
    assert run.status == RunStatus.FAILED
    assert run.error == (
        "Mapping messy.csv failed: The model call failed: AuthenticationError: invalid API key"
    )
    assert len(llm.calls) == 1


def test_step_limit_fails_the_run(settings, case):
    path, _ = case
    endless = FakeLLM(lambda messages: FakeReply(content="still thinking"))
    run, _ = run_onboard(settings, endless, path)
    assert run.status == RunStatus.FAILED
    assert f"Reached the step limit ({settings.agent_max_steps})" in run.error
    assert run.model_calls == settings.agent_max_steps


def test_rejected_submission_is_retried_and_stored_once(settings, case):
    path, key = case
    oracle = oracle_responder(key)
    attempts: list[int] = []

    def responder(messages):
        step = oracle(messages)
        names = [name for name, _ in step.tool_calls]
        if names == ["submit_mappings"] and not attempts:
            attempts.append(1)
            bad = json.loads(json.dumps(step.tool_calls[0][1]))
            bad["mappings"][0]["transform_sql"] = "DROP TABLE raw.messy"
            return FakeReply(tool_calls=[("submit_mappings", bad)])
        return step

    run, _ = run_onboard(settings, FakeLLM(responder), path)
    assert run.status == RunStatus.AWAITING_REVIEW
    assert run.model_calls == 3
    assert len(run.mappings) == len(key["mappings"]) + len(key["missing_target_columns"])


def test_unreadable_input_fails_during_profiling(settings, tmp_path):
    bad = tmp_path / "export.xlsx"
    bad.write_text("x")
    run, _ = run_onboard(settings, FakeLLM([]), bad)
    assert run.status == RunStatus.FAILED
    assert "Unsupported file type '.xlsx'" in run.error
