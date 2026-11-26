"""Run one onboarding through Profile, Map, review, and Build, persisting state at every step.

``onboard`` stops at ``awaiting_review`` unless a reviewer is passed (the CLI's --approve-all,
or the eval harness answering from the answer key); then it builds in the same trace and ends
in ``done`` or ``failed`` (with the reason in ``runs.error``). ``build_run`` builds a run that
was reviewed later, in its own trace.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session

from agent.llm import LLM
from agent.loop import LoopResult
from agent.phases import MapOutcome, build_phase, map_phase, profile_phase
from agent.settings import Settings
from agent.tracing import Tracer
from api.models import (
    ColumnProfile,
    Defect,
    Mapping,
    MappingStatus,
    Run,
    RunStatus,
    SourceFile,
)
from datagen.schemas import load_schema
from tools.profiling import FileProfile
from tools.warehouse import FileInfo, ToolError, Warehouse

Reviewer = Callable[[Session, Run, Warehouse], None]


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


def _set_status(session: Session, run: Run, status: RunStatus, error: str | None = None) -> None:
    run.status = status
    run.error = error
    session.commit()


def _store_profiles(
    session: Session, run: Run, profiled: list[tuple[FileInfo, FileProfile]]
) -> dict[str, SourceFile]:
    stored = {}
    for file, profile in profiled:
        source = SourceFile(
            run_id=run.id,
            warehouse_file_id=file.file_id,
            filename=file.filename,
            row_count=file.row_count,
            sha256=file.sha256,
        )
        source.profiles = [
            ColumnProfile(
                column_name=c.column_name,
                position=c.position,
                inferred_type=c.inferred_type,
                null_rate=c.null_rate,
                distinct_count=c.distinct_count,
                sample_values=[t.model_dump() for t in c.top_values],
                patterns=c.patterns,
            )
            for c in profile.columns
        ]
        session.add(source)
        stored[file.file_id] = source
    session.commit()
    return stored


def _store_outcome(session: Session, run: Run, source: SourceFile, outcome: MapOutcome) -> None:
    for m in outcome.mappings:
        session.add(
            Mapping(
                run_id=run.id,
                file_id=source.id,
                source_column=m.source_column,
                target_table=run.target_schema,
                target_column=m.target_column,
                transform_sql=m.transform_sql,
                confidence=m.confidence,
                reason=m.reason,
                status=MappingStatus.PROPOSED if m.needs_review else MappingStatus.APPROVED,
                review_reason=m.review_reason,
            )
        )
    for missing in outcome.missing_required:
        session.add(
            Mapping(
                run_id=run.id,
                file_id=source.id,
                source_column=None,
                target_table=run.target_schema,
                target_column=missing.target_column,
                transform_sql=None,
                confidence=None,
                reason=missing.reason,
                status=MappingStatus.PROPOSED,
                review_reason="required target column has no source column",
            )
        )
    for d in outcome.defects:
        session.add(
            Defect(
                run_id=run.id,
                file_id=source.id,
                column_name=d.source_column,
                target_column=d.target_column,
                defect_type=str(d.defect_type),
                affected_rows=d.affected_rows,
                description=d.description,
                proposed_fix=d.proposed_fix,
            )
        )
    session.commit()


def _add_loop_stats(run: Run, loop: LoopResult | None) -> None:
    if loop is not None:
        run.model_calls += loop.steps
        run.tool_calls += loop.tool_calls
        run.prompt_tokens += loop.prompt_tokens
        run.completion_tokens += loop.completion_tokens


def _build(
    session: Session,
    settings: Settings,
    llm: LLM | None,
    tracer: Tracer,
    run: Run,
    wh: Warehouse,
    retries: int,
    sleep: Callable[[float], None],
) -> None:
    """Build an awaiting-review run whose mappings are all decided. Raises _RunFailed."""
    session.expire(run, ["mappings", "files", "defects"])
    pending = [m for m in run.mappings if m.status == MappingStatus.PROPOSED]
    if pending:
        raise _RunFailed(f"{len(pending)} mappings still need review before the build")
    _set_status(session, run, RunStatus.BUILDING)
    with tracer.span("phase:build", "CHAIN"):
        outcome = build_phase(
            session, run, wh, llm, load_schema(run.target_schema),
            max_repairs=settings.build_max_repairs, max_steps=settings.agent_max_steps,
            tracer=tracer, retries=retries, backoff_seconds=settings.llm_retry_backoff_seconds,
            sleep=sleep,
        )  # fmt: skip
    _add_loop_stats(run, outcome.loop)
    run.build_report = {
        "status": outcome.status,
        "repairs": outcome.repairs,
        "dbt": outcome.result.model_dump(),
        "row_accounting": [{**a.model_dump(), "balanced": a.balanced} for a in outcome.accounting],
    }
    if outcome.status != "passed":
        raise _RunFailed(outcome.reason or "build failed")
    _set_status(session, run, RunStatus.DONE)


def onboard(
    session: Session,
    settings: Settings,
    llm: LLM,
    tracer: Tracer,
    *,
    customer_name: str,
    schema_name: str,
    paths: list[Path],
    run_id: str | None = None,
    review: Reviewer | None = None,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> Run:
    schema = load_schema(schema_name)
    run = Run(
        id=run_id or new_run_id(),
        customer_name=customer_name,
        target_schema=schema_name,
        model_name=llm.model,
    )
    session.add(run)
    session.commit()
    started = time.monotonic()
    wh = Warehouse.for_run(run.id, settings.data_dir)

    with tracer.span(
        "run", "AGENT", inputs={"run_id": run.id, "schema": schema_name, "files": len(paths)}
    ) as root:
        tracer.tag_trace({"run_id": run.id, "model": llm.model, "customer": customer_name})
        run.trace_id = tracer.trace_id
        try:
            _set_status(session, run, RunStatus.PROFILING)
            with tracer.span("phase:profile", "CHAIN"):
                profiled = profile_phase(wh, paths)
            sources = _store_profiles(session, run, profiled)

            _set_status(session, run, RunStatus.MAPPING)
            for file, profile in profiled:
                with tracer.span(f"phase:map:{file.filename}", "CHAIN"):
                    outcome = map_phase(
                        llm, wh, file, profile, schema,
                        threshold=settings.review_confidence_threshold,
                        max_steps=settings.agent_max_steps, tracer=tracer,
                        retries=retries, backoff_seconds=settings.llm_retry_backoff_seconds,
                        sleep=sleep,
                    )  # fmt: skip
                _add_loop_stats(run, outcome.loop)
                if outcome.status != "submitted":
                    raise _RunFailed(f"Mapping {file.filename} failed: {outcome.reason}")
                _store_outcome(session, run, sources[file.file_id], outcome)
            _set_status(session, run, RunStatus.AWAITING_REVIEW)
            if review is not None:
                review(session, run, wh)
                _build(session, settings, llm, tracer, run, wh, retries, sleep)
        except (ToolError, _RunFailed) as exc:
            _set_status(session, run, RunStatus.FAILED, str(exc))
        run.duration_seconds = round(time.monotonic() - started, 2)
        session.commit()
        root.set_outputs({"status": run.status, "error": run.error})
    tracer.flush()
    return run


class _RunFailed(Exception):
    pass


def build_run(
    session: Session,
    settings: Settings,
    llm: LLM | None,
    tracer: Tracer,
    run: Run,
    *,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> Run:
    """Build a run that was reviewed after onboarding. Rebuilding replaces the dbt tables."""
    if run.status not in (RunStatus.AWAITING_REVIEW, RunStatus.FAILED, RunStatus.DONE):
        raise ToolError(f"Run {run.id} is {run.status}; it cannot be built now")
    wh = Warehouse.for_run(run.id, settings.data_dir)
    started = time.monotonic()
    with tracer.span("build", "AGENT", inputs={"run_id": run.id}) as root:
        tracer.tag_trace({"run_id": run.id, "model": llm.model if llm else "none"})
        try:
            _build(session, settings, llm, tracer, run, wh, retries, sleep)
        except (ToolError, _RunFailed) as exc:
            _set_status(session, run, RunStatus.FAILED, str(exc))
        run.duration_seconds = round((run.duration_seconds or 0) + time.monotonic() - started, 2)
        session.commit()
        root.set_outputs({"status": run.status, "error": run.error})
    tracer.flush()
    return run
