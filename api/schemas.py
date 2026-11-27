"""Request and response bodies of the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from agent.review import Decision, pending_reviews
from api.models import Mapping, MappingContract, Run, RunStatus


class CreateRun(BaseModel):
    customer_name: str = Field(min_length=1, max_length=200)
    target_schema: str = Field(description="customers, subscriptions, invoices, service_requests")


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
    run_type: str  # onboarding or recurring
    contract_id: int | None  # recurring loads: the contract version they were checked against
    saved_contract_id: int | None  # onboarding runs saved as a source: that contract version
    drift: list[dict[str, Any]] | None  # recurring loads: agent.drift.Finding dicts
    error: str | None
    model_name: str | None
    trace_id: str | None
    files: list[FileOut]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, run: Run, *, busy: bool, saved_contract_id: int | None = None) -> RunOut:
        return cls(
            id=run.id,
            customer_name=run.customer_name,
            target_schema=run.target_schema,
            status=run.status,
            phase=PHASES.get(run.status, run.status),
            steps=run.model_calls,
            pending_reviews=len(pending_reviews(run)),
            busy=busy,
            run_type=run.run_type,
            contract_id=run.contract_id,
            saved_contract_id=saved_contract_id,
            drift=run.drift,
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


class SaveContract(BaseModel):
    source_name: str = Field(min_length=1, max_length=100)


class LastLoad(BaseModel):
    run_id: str
    status: str
    created_at: datetime


class ContractSummary(BaseModel):
    id: int
    customer_name: str
    source_name: str
    target_schema: str
    version: int
    created_at: datetime
    fingerprint: str
    loads: int  # recurring loads of this source, all versions
    last_load: LastLoad | None
    table_rows: int | None  # rows in the source's table


class ContractVersion(BaseModel):
    id: int
    version: int
    created_at: datetime
    created_from_run_id: str


class ContractOut(ContractSummary):
    created_from_run_id: str
    row_count: int  # rows of the baseline file
    mappings: list[dict[str, Any]]
    columns: list[dict[str, str]]
    versions: list[ContractVersion]
    recent_loads: list[RunOut]


def contract_summary(
    contract: MappingContract, loads: list[Run], table_rows: int | None
) -> dict[str, Any]:
    last = loads[0] if loads else None
    return {
        "id": contract.id,
        "customer_name": contract.customer_name,
        "source_name": contract.source_name,
        "target_schema": contract.target_schema,
        "version": contract.version,
        "created_at": contract.created_at,
        "fingerprint": contract.fingerprint,
        "loads": len(loads),
        "last_load": (
            LastLoad(run_id=last.id, status=last.status, created_at=last.created_at)
            if last
            else None
        ),
        "table_rows": table_rows,
    }
