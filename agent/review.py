"""Human review decisions on proposed mappings, recorded in ``review_events``."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.models import Mapping, MappingStatus, ReviewEvent, Run, SourceFile
from tools.transforms import validate_expression
from tools.warehouse import ToolError


class Decision(BaseModel):
    mapping_id: int
    action: Literal["approve", "edit", "reject"]
    transform_sql: str | None = None  # required for "edit"


def mapping_state(m: Mapping) -> dict[str, object]:
    return {
        "source_column": m.source_column,
        "target_column": m.target_column,
        "transform_sql": m.transform_sql,
        "status": m.status,
    }


def source_columns(source: SourceFile) -> list[str]:
    """The file's columns in order, from its stored profiles."""
    return [p.column_name for p in source.profiles]


def validated_sql(source: SourceFile, sql: str) -> str:
    """Check an expression against the source file's columns; return the SQL to store."""
    return validate_expression(sql, source_columns(source))


def apply_review(session: Session, run: Run, decisions: Iterable[Decision], actor: str) -> None:
    by_id = {m.id: m for m in run.mappings}
    files = {f.id: f for f in run.files}
    for decision in decisions:
        mapping = by_id.get(decision.mapping_id)
        if mapping is None:
            raise ToolError(f"Mapping {decision.mapping_id} does not belong to run {run.id}")
        before = mapping_state(mapping)
        if decision.action == "approve":
            mapping.status = MappingStatus.APPROVED
        elif decision.action == "reject":
            mapping.status = MappingStatus.REJECTED
        else:
            if not decision.transform_sql or mapping.source_column is None:
                raise ToolError("An edit needs transform_sql and a mapping with a source column")
            try:
                mapping.transform_sql = validated_sql(
                    files[mapping.file_id], decision.transform_sql
                )
            except ToolError as exc:
                raise ToolError(f"Edit to {mapping.target_column}: {exc}") from exc
            mapping.status = MappingStatus.EDITED
        session.add(
            ReviewEvent(
                run_id=run.id,
                item_type="mapping",
                item_id=mapping.id,
                action=decision.action,
                before=before,
                after={**mapping_state(mapping), "actor": actor},
            )
        )
    session.commit()


def pending_reviews(run: Run) -> list[Mapping]:
    return [m for m in run.mappings if m.status == MappingStatus.PROPOSED]


def finish_review(session: Session, run: Run) -> int:
    """Mark the review done when nothing is pending; returns how many mappings still are."""
    pending = len(pending_reviews(run))
    if pending == 0 and run.reviewed_at is None:
        run.reviewed_at = datetime.now(UTC)
        session.commit()
    return pending


def approve_all(session: Session, run: Run, actor: str = "cli") -> None:
    """Approve every mapping still waiting for review (the CLI's --approve-all)."""
    decisions = [Decision(mapping_id=m.id, action="approve") for m in pending_reviews(run)]
    apply_review(session, run, decisions, actor)
