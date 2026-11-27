"""Request and response bodies of the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from agent.review import Decision, pending_reviews
from api.models import Mapping, Run, RunStatus


class CreateRun(BaseModel):
    customer_name: str = Field(min_length=1, max_length=200)
    target_schema: str = Field(description="customers, subscriptions, or invoices")


class FileOut(BaseModel):
    id: int
    filename: str
    row_count: int
    profiled: bool


class RunOut(BaseModel):
    id: str
    customer_name: str
    target_schema: str
    status: str
    phase: str
    steps: int  # model calls so far
    pending_reviews: int
    busy: bool  # the background worker holds the run (queued or running); poll until false
    error: str | None
    model_name: str | None
    trace_id: str | None
    files: list[FileOut]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, run: Run, *, busy: bool) -> RunOut:
        return cls(
            id=run.id,
            customer_name=run.customer_name,
            target_schema=run.target_schema,
            status=run.status,
            phase=PHASES.get(run.status, run.status),
            steps=run.model_calls,
            pending_reviews=len(pending_reviews(run)),
            busy=busy,
            error=run.error,
            model_name=run.model_name,
            trace_id=run.trace_id,
            files=[
                FileOut(
                    id=f.id, filename=f.filename, row_count=f.row_count, profiled=bool(f.profiles)
                )
                for f in run.files
            ],
            created_at=run.created_at,
            updated_at=run.updated_at,
        )


PHASES = {
    RunStatus.CREATED: "upload files, then POST /start",
    RunStatus.PROFILING: "profiling",
    RunStatus.MAPPING: "mapping",
    RunStatus.AWAITING_REVIEW: "waiting for review",
    RunStatus.BUILDING: "building",
    RunStatus.DONE: "done",
    RunStatus.FAILED: "failed; POST /start to resume",
}


class MappingOut(BaseModel):
    id: int
    file_id: int
    source_column: str | None
    target_table: str
    target_column: str
    transform_sql: str | None
    confidence: float | None
    reason: str
    status: str
    needs_review: bool
    review_reason: str | None
    sample_values: list[Any]  # top values of the source column, from its profile

    @classmethod
    def of(cls, m: Mapping, samples: dict[tuple[int, str], list[Any]]) -> MappingOut:
        return cls(
            id=m.id,
            file_id=m.file_id,
            source_column=m.source_column,
            target_table=m.target_table,
            target_column=m.target_column,
            transform_sql=m.transform_sql,
            confidence=m.confidence,
            reason=m.reason,
            status=m.status,
            needs_review=m.review_reason is not None,
            review_reason=m.review_reason,
            sample_values=samples.get((m.file_id, m.source_column or ""), []),
        )


class DefectOut(BaseModel):
    id: int
    file_id: int
    column_name: str | None
    target_column: str | None
    defect_type: str
    affected_rows: int | None
    description: str
    proposed_fix: str
    status: str


class ReviewIn(BaseModel):
    decisions: list[Decision] = Field(default_factory=list)
    build: bool = Field(default=True, description="Start the build once nothing is pending")


class ReviewOut(BaseModel):
    pending_reviews: int
    building: bool
    run: RunOut


class ReportOut(BaseModel):
    run: RunOut
    duration_seconds: float | None
    model_calls: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    trace_id: str | None
    trace_url: str | None
    build: dict[str, Any] | None  # dbt results, repairs, row accounting with reject reasons
