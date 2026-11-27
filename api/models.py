"""App-state tables (spec §9)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    CREATED = "created"
    PROFILING = "profiling"
    MAPPING = "mapping"
    AWAITING_REVIEW = "awaiting_review"
    BUILDING = "building"
    DONE = "done"
    FAILED = "failed"


class MappingStatus(StrEnum):
    PROPOSED = "proposed"  # waiting for a human decision
    APPROVED = "approved"
    EDITED = "edited"
    REJECTED = "rejected"


class RunType(StrEnum):
    ONBOARDING = "onboarding"  # a new source: map everything, a person reviews
    RECURRING = "recurring"  # a new file from a saved source (mapping contract)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_name: Mapped[str] = mapped_column(String(200))
    target_schema: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.CREATED)
    run_type: Mapped[str] = mapped_column(String(32), default="onboarding")
    contract_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    trace_experiment_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # dbt results, row accounting per file, and repair count from the Build phase
    build_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # drift checks of a recurring load (agent.drift.Finding dicts); NULL until they ran
    drift: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # set when review decisions are submitted; a resumed run with it set goes on to build
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    files: Mapped[list[SourceFile]] = relationship(back_populates="run", order_by="SourceFile.id")
    mappings: Mapped[list[Mapping]] = relationship(order_by="Mapping.id")
    defects: Mapped[list[Defect]] = relationship(order_by="Defect.id")


class SourceFile(Base):
    __tablename__ = "source_files"
    __table_args__ = (UniqueConstraint("run_id", "warehouse_file_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    warehouse_file_id: Mapped[str] = mapped_column(String(32))  # file_id used by the tools
    filename: Mapped[str] = mapped_column(String(500))
    row_count: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    raw_table: Mapped[str] = mapped_column(String(200))  # raw.<raw_table> in the warehouse

    run: Mapped[Run] = relationship(back_populates="files")
    profiles: Mapped[list[ColumnProfile]] = relationship(order_by="ColumnProfile.position")


class ColumnProfile(Base):
    __tablename__ = "column_profiles"
    __table_args__ = (UniqueConstraint("file_id", "column_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id"))
    column_name: Mapped[str] = mapped_column(String(500))
    position: Mapped[int] = mapped_column(Integer)
    inferred_type: Mapped[str] = mapped_column(String(32))
    null_rate: Mapped[float] = mapped_column(Float)
    distinct_count: Mapped[int] = mapped_column(Integer)
    sample_values: Mapped[list[Any]] = mapped_column(JSON)  # top values with counts
    patterns: Mapped[dict[str, Any]] = mapped_column(JSON)
    profile: Mapped[dict[str, Any]] = mapped_column(JSON)  # the full tool profile


class Mapping(Base):
    """One proposed source-to-target mapping. A required target with no source column is a
    row with ``source_column`` NULL, waiting for review."""

    __tablename__ = "mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id"))
    source_column: Mapped[str | None] = mapped_column(String(500), nullable=True)
    target_table: Mapped[str] = mapped_column(String(100))
    target_column: Mapped[str] = mapped_column(String(100))
    transform_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # NULL = auto-approved


class Defect(Base):
    __tablename__ = "defects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id"))
    column_name: Mapped[str | None] = mapped_column(String(500), nullable=True)  # source column
    target_column: Mapped[str | None] = mapped_column(String(100), nullable=True)
    defect_type: Mapped[str] = mapped_column(String(64))
    affected_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str] = mapped_column(Text)
    proposed_fix: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="open")


class ReviewEvent(Base):
    __tablename__ = "review_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    item_type: Mapped[str] = mapped_column(String(32))  # "mapping" or "defect"
    item_id: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(32))  # approve, edit, reject, agent_repair
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MappingContract(Base):
    """One version of a saved source: the approved mappings of a run, with the fingerprint and
    baseline profile that later files from the same source are compared with."""

    __tablename__ = "mapping_contracts"
    __table_args__ = (UniqueConstraint("customer_name", "source_name", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_name: Mapped[str] = mapped_column(String(200))
    source_name: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer)
    target_schema: Mapped[str] = mapped_column(String(100))
    fingerprint: Mapped[str] = mapped_column(String(64))  # sha256 of sorted name:inferred_type
    baseline_profile: Mapped[dict[str, Any]] = mapped_column(JSON)
    mappings: Mapped[list[dict[str, Any]]] = mapped_column(JSON)  # source/target/transform_sql
    created_from_run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
