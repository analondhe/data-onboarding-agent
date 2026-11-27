"""Human review decisions on proposed mappings, recorded in ``review_events``."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.models import Mapping, MappingStatus, ReviewEvent, Run
from tools.transforms import validate_expression
from tools.warehouse import ToolError, Warehouse, get_file, source_columns


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


def validated_sql(wh: Warehouse, file_key: str, sql: str) -> str:
    """Check an expression against the source file's columns; return the SQL to store."""
    with wh.connect(read_only=True) as con:
        columns = source_columns(con, get_file(con, file_key))
    return validate_expression(sql, columns)


def apply_review(
    session: Session, run: Run, wh: Warehouse, decisions: Iterable[Decision], actor: str
) -> None:
    by_id = {m.id: m for m in run.mappings}
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
            file_key = next(f.warehouse_file_id for f in run.files if f.id == mapping.file_id)
            mapping.transform_sql = validated_sql(wh, file_key, decision.transform_sql)
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


def approve_all(session: Session, run: Run, wh: Warehouse, actor: str = "cli") -> None:
    """Approve every mapping still waiting for review (the CLI's --approve-all)."""
    pending = [m for m in run.mappings if m.status == MappingStatus.PROPOSED]
    decisions = [Decision(mapping_id=m.id, action="approve") for m in pending]
    apply_review(session, run, wh, decisions, actor)
