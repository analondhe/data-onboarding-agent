"""Run one onboarding through Profile and Map, persisting state at every step.

The run ends in ``awaiting_review`` (mappings and defects stored) or ``failed`` (with the
reason in ``runs.error``). The Build phase and resume come in later milestones.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session

from agent.llm import LLM
from agent.phases import MapOutcome, map_phase, profile_phase
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
                if outcome.loop is not None:
                    run.model_calls += outcome.loop.steps
                    run.tool_calls += outcome.loop.tool_calls
                    run.prompt_tokens += outcome.loop.prompt_tokens
                    run.completion_tokens += outcome.loop.completion_tokens
                if outcome.status != "submitted":
                    raise _RunFailed(f"Mapping {file.filename} failed: {outcome.reason}")
                _store_outcome(session, run, sources[file.file_id], outcome)
            _set_status(session, run, RunStatus.AWAITING_REVIEW)
        except (ToolError, _RunFailed) as exc:
            _set_status(session, run, RunStatus.FAILED, str(exc))
        run.duration_seconds = round(time.monotonic() - started, 2)
        session.commit()
        root.set_outputs({"status": run.status, "error": run.error})
    tracer.flush()
    return run


class _RunFailed(Exception):
    pass
