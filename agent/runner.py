"""The run state machine: created -> profiling -> mapping -> awaiting_review -> building -> done.

``advance`` runs every phase that is due and skips the ones already finished, so a failed run
resumes from its last completed phase:

- a file without stored profiles is profiled;
- a file without mappings is mapped (files mapped earlier keep their mappings);
- once review is finished (``runs.reviewed_at``), the run is built.

The API calls ``advance`` from its background worker; the CLI and evals call ``onboard``, which
creates the run, loads the files, and advances it with an automatic reviewer.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session

from agent.llm import LLM
from agent.loop import LoopResult
from agent.phases import MapOutcome, build_phase, map_phase
from agent.review import finish_review, pending_reviews
from agent.settings import Settings
from agent.tool_backend import RunTools, open_tools
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
from tools.profiling import ColumnProfile as ToolColumnProfile
from tools.profiling import FileProfile
from tools.warehouse import FileInfo, ToolError, Warehouse, load_file

Reviewer = Callable[[Session, Run], None]
ACTIVE = (RunStatus.PROFILING, RunStatus.MAPPING, RunStatus.BUILDING)


class RunFailed(Exception):
    """A phase failed for a reason the run should record."""


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


def _set_status(session: Session, run: Run, status: RunStatus, error: str | None = None) -> None:
    run.status = status
    run.error = error
    session.commit()


def create_run(
    session: Session,
    *,
    customer_name: str,
    schema_name: str,
    model_name: str | None = None,
    run_id: str | None = None,
) -> Run:
    load_schema(schema_name)  # unknown schemas fail before anything is stored
    run = Run(
        id=run_id or new_run_id(),
        customer_name=customer_name,
        target_schema=schema_name,
        model_name=model_name,
    )
    session.add(run)
    session.commit()
    return run


def add_file(session: Session, settings: Settings, run: Run, path: Path) -> SourceFile:
    """Load a file into the run's warehouse and record it. Re-adding the same content returns
    the existing file. Raises ToolError for files that cannot be loaded."""
    info = load_file(Warehouse.for_run(run.id, settings.data_dir), path)
    for source in run.files:
        if source.warehouse_file_id == info.file_id:
            return source
    source = SourceFile(
        run_id=run.id,
        warehouse_file_id=info.file_id,
        filename=info.filename,
        row_count=info.row_count,
        sha256=info.sha256,
        raw_table=info.table_name,
    )
    session.add(source)
    session.commit()
    session.expire(run, ["files"])
    return source


# --- Phases -----------------------------------------------------------------------------------


def _profile(session: Session, run: Run, tools: RunTools, tracer: Tracer) -> None:
    for source in run.files:
        if source.profiles:
            continue
        with tracer.span(f"phase:profile:{source.filename}", "CHAIN"):
            profile = tools.profile_file(source.warehouse_file_id)
        source.profiles = [
            ColumnProfile(
                column_name=c.column_name,
                position=c.position,
                inferred_type=c.inferred_type,
                null_rate=c.null_rate,
                distinct_count=c.distinct_count,
                sample_values=[t.model_dump() for t in c.top_values],
                patterns=c.patterns,
                profile=c.model_dump(),
            )
            for c in profile.columns
        ]
        session.commit()


def _stored_profile(source: SourceFile) -> tuple[FileInfo, FileProfile]:
    file = FileInfo(
        file_id=source.warehouse_file_id,
        filename=source.filename,
        table_name=source.raw_table,
        row_count=source.row_count,
        sha256=source.sha256,
    )
    profile = FileProfile(
        file_id=source.warehouse_file_id,
        filename=source.filename,
        row_count=source.row_count,
        columns=[ToolColumnProfile.model_validate(p.profile) for p in source.profiles],
    )
    return file, profile


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
    session.expire(run, ["mappings", "defects"])


def _add_loop_stats(run: Run, loop: LoopResult | None) -> None:
    if loop is not None:
        run.model_calls += loop.steps
        run.tool_calls += loop.tool_calls
        run.prompt_tokens += loop.prompt_tokens
        run.completion_tokens += loop.completion_tokens


def _map(
    session: Session,
    settings: Settings,
    llm: LLM,
    tracer: Tracer,
    run: Run,
    tools: RunTools,
    retries: int,
    sleep: Callable[[float], None],
) -> None:
    schema = load_schema(run.target_schema)
    mapped = {m.file_id for m in run.mappings}
    for source in run.files:
        if source.id in mapped:
            continue
        file, profile = _stored_profile(source)
        with tracer.span(f"phase:map:{source.filename}", "CHAIN"):
            outcome = map_phase(
                llm, tools, file, profile, schema,
                threshold=settings.review_confidence_threshold,
                max_steps=settings.agent_max_steps, tracer=tracer, retries=retries,
                backoff_seconds=settings.llm_retry_backoff_seconds, sleep=sleep,
            )  # fmt: skip
        _add_loop_stats(run, outcome.loop)
        if outcome.status != "submitted":
            raise RunFailed(f"Mapping {source.filename} failed: {outcome.reason}")
        _store_outcome(session, run, source, outcome)


def _build(
    session: Session,
    settings: Settings,
    llm: LLM | None,
    tracer: Tracer,
    run: Run,
    tools: RunTools,
    retries: int,
    sleep: Callable[[float], None],
) -> None:
    if pending := pending_reviews(run):
        raise RunFailed(f"{len(pending)} mappings still need review before the build")
    _set_status(session, run, RunStatus.BUILDING)
    with tracer.span("phase:build", "CHAIN"):
        outcome = build_phase(
            session, run, Warehouse.for_run(run.id, settings.data_dir), tools, llm,
            load_schema(run.target_schema), max_repairs=settings.build_max_repairs,
            max_steps=settings.agent_max_steps, tracer=tracer, retries=retries,
            backoff_seconds=settings.llm_retry_backoff_seconds, sleep=sleep,
        )  # fmt: skip
    _add_loop_stats(run, outcome.loop)
    run.build_report = {
        "status": outcome.status,
        "repairs": outcome.repairs,
        "dbt": outcome.result.model_dump(),
        "row_accounting": [{**a.model_dump(), "balanced": a.balanced} for a in outcome.accounting],
    }
    if outcome.status != "passed":
        raise RunFailed(outcome.reason or "build failed")
    _set_status(session, run, RunStatus.DONE)


# --- Driving a run ----------------------------------------------------------------------------


def advance(
    session: Session,
    settings: Settings,
    llm: LLM,
    tracer: Tracer,
    run: Run,
    *,
    review: Reviewer | None = None,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> Run:
    """Run every phase that is due, under one trace. Ends at awaiting_review (a human must
    decide), done, or failed with the reason in ``runs.error``."""
    if run.status in ACTIVE:
        raise ToolError(f"Run {run.id} is already {run.status}")
    if not run.files:
        raise ToolError(f"Run {run.id} has no files; upload at least one first")
    run.model_name = llm.model
    started = time.monotonic()
    with tracer.span("run", "AGENT", inputs={"run_id": run.id, "from": run.status}) as root:
        tracer.tag_trace({"run_id": run.id, "model": llm.model, "customer": run.customer_name})
        if tracer.trace_id:
            run.trace_id, run.trace_experiment_id = tracer.trace_id, tracer.experiment_id
        try:
            with open_tools(settings, Warehouse.for_run(run.id, settings.data_dir)) as tools:
                if any(not f.profiles for f in run.files):
                    _set_status(session, run, RunStatus.PROFILING)
                    _profile(session, run, tools, tracer)
                if {f.id for f in run.files} - {m.file_id for m in run.mappings}:
                    _set_status(session, run, RunStatus.MAPPING)
                    _map(session, settings, llm, tracer, run, tools, retries, sleep)
                _set_status(session, run, RunStatus.AWAITING_REVIEW)
                if review is not None:
                    review(session, run)
                    finish_review(session, run)
                if run.reviewed_at is not None:
                    _build(session, settings, llm, tracer, run, tools, retries, sleep)
        except (ToolError, RunFailed) as exc:
            _set_status(session, run, RunStatus.FAILED, str(exc))
        run.duration_seconds = round((run.duration_seconds or 0) + time.monotonic() - started, 2)
        session.commit()
        root.set_outputs({"status": run.status, "error": run.error})
    tracer.flush()
    return run


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
    """Create a run, load its files, and advance it (the CLI and eval path)."""
    run = create_run(
        session,
        customer_name=customer_name,
        schema_name=schema_name,
        model_name=llm.model,
        run_id=run_id,
    )
    try:
        for path in paths:
            add_file(session, settings, run, path)
    except ToolError as exc:
        _set_status(session, run, RunStatus.FAILED, str(exc))
        return run
    return advance(session, settings, llm, tracer, run, review=review, retries=retries, sleep=sleep)


def recover_interrupted(session: Session) -> list[str]:
    """Mark runs left mid-phase by a crash or restart as failed, so they can be resumed."""
    runs = session.query(Run).filter(Run.status.in_([s.value for s in ACTIVE])).all()
    for run in runs:
        _set_status(
            session,
            run,
            RunStatus.FAILED,
            f"Interrupted while {run.status}; POST /runs/{run.id}/start to resume",
        )
    return [r.id for r in runs]
