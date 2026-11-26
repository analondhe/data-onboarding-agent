"""The ``submit_mappings`` payload, its validation, and review routing."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent.defects import DefectType
from tools.transforms import UnsafeSQLError, referenced_columns, validate_expression


class ProposedMapping(BaseModel):
    source_column: str = Field(description="Column in the source file")
    target_column: str = Field(description="Column in the target schema")
    transform_sql: str = Field(
        description="One DuckDB expression over a single source row producing the target value"
    )
    confidence: float = Field(ge=0, le=1, description="0 to 1; below the threshold goes to review")
    reason: str = Field(min_length=1, description="Why this source column is this target column")


class MissingTarget(BaseModel):
    target_column: str = Field(description="Required target column with no source column")
    reason: str = Field(min_length=1)


class ProposedDefect(BaseModel):
    defect_type: DefectType
    source_column: str | None = Field(default=None, description="Affected source column, if any")
    target_column: str | None = Field(default=None, description="Affected target column, if any")
    affected_rows: int | None = Field(default=None, ge=0, description="Estimated rows affected")
    description: str = Field(min_length=1, description="What is wrong")
    proposed_fix: str = Field(default="", description="How the transform handles it")


class MappingSubmission(BaseModel):
    mappings: list[ProposedMapping]
    missing_required: list[MissingTarget] = Field(
        default_factory=list,
        description="Required target columns that no source column provides",
    )
    defects: list[ProposedDefect] = Field(default_factory=list)


class ReviewedMapping(ProposedMapping):
    needs_review: bool
    review_reason: str | None


def validate_submission(
    submission: MappingSubmission, schema: dict[str, Any], source_columns: list[str]
) -> tuple[MappingSubmission, list[str]]:
    """Return the submission with normalized SQL, and every problem the model must fix."""
    targets = {c["name"]: c for c in schema["columns"]}
    target_list = ", ".join(targets)
    errors: list[str] = []
    seen_targets: dict[str, str] = {}
    normalized: list[ProposedMapping] = []

    for m in submission.mappings:
        label = f"mapping {m.source_column!r} -> {m.target_column!r}"
        if m.target_column not in targets:
            errors.append(f"{label}: unknown target column. Target columns are: {target_list}")
        elif m.target_column in seen_targets:
            errors.append(
                f"{label}: target {m.target_column!r} is already mapped from "
                f"{seen_targets[m.target_column]!r}; each target takes one source column"
            )
        else:
            seen_targets[m.target_column] = m.source_column
        if m.source_column not in source_columns:
            errors.append(
                f"{label}: unknown source column. Source columns are: {', '.join(source_columns)}"
            )
            normalized.append(m)
            continue
        try:
            sql = validate_expression(m.transform_sql, source_columns)
        except UnsafeSQLError as exc:
            errors.append(f"{label}: transform_sql refused: {exc}")
            normalized.append(m)
            continue
        if m.source_column not in referenced_columns(sql, source_columns):
            errors.append(f"{label}: transform_sql must read its source column {m.source_column!r}")
        normalized.append(m.model_copy(update={"transform_sql": sql}))

    declared_missing = set()
    for missing in submission.missing_required:
        if missing.target_column not in targets:
            errors.append(
                f"missing_required: unknown target column {missing.target_column!r}. "
                f"Target columns are: {target_list}"
            )
        elif missing.target_column in seen_targets:
            errors.append(
                f"missing_required: {missing.target_column!r} is also mapped; remove one of them"
            )
        declared_missing.add(missing.target_column)

    for name, column in targets.items():
        if column.get("required") and name not in seen_targets and name not in declared_missing:
            errors.append(
                f"required target column {name!r} has no mapping. Map it, or list it in "
                "missing_required with a reason if no source column holds it"
            )

    defects = [_normalize_defect(d, source_columns, targets) for d in submission.defects]
    return submission.model_copy(update={"mappings": normalized, "defects": defects}), errors


def _normalize_defect(
    defect: ProposedDefect, source_columns: list[str], targets: dict[str, Any]
) -> ProposedDefect:
    """Defects are informational, so a misplaced column name is fixed rather than rejected:
    a source name given as the target moves over, and names that match nothing are dropped."""
    source, target = defect.source_column, defect.target_column
    if target is not None and target not in targets and target in source_columns:
        source, target = source or target, None
    if source is not None and source not in source_columns and source in targets:
        source, target = None, target or source
    return defect.model_copy(
        update={
            "source_column": source if source in source_columns else None,
            "target_column": target if target in targets else None,
        }
    )


def route_for_review(
    submission: MappingSubmission, threshold: float
) -> tuple[list[ReviewedMapping], list[ProposedDefect]]:
    """Mark low-confidence mappings for review and derive structural defects.

    A source column mapped to two targets is a merged column, and every declared missing
    required target is a missing-column defect; both are added if the model did not report them.
    """
    reviewed = [
        ReviewedMapping(
            **m.model_dump(),
            needs_review=m.confidence < threshold,
            review_reason=(
                f"confidence {m.confidence:.2f} is below {threshold:.2f}"
                if m.confidence < threshold
                else None
            ),
        )
        for m in submission.mappings
    ]
    defects = list(submission.defects)
    # A defect counts as reported for every column it names, source or target.
    reported = {(d.defect_type, d.source_column) for d in defects} | {
        (d.defect_type, d.target_column) for d in defects
    }

    targets_by_source: dict[str, list[str]] = {}
    for m in submission.mappings:
        targets_by_source.setdefault(m.source_column, []).append(m.target_column)
    for source, mapped in targets_by_source.items():
        if len(mapped) > 1 and (DefectType.MERGED_COLUMN, source) not in reported:
            defects.append(
                ProposedDefect(
                    defect_type=DefectType.MERGED_COLUMN,
                    source_column=source,
                    description=f"{source!r} holds {', '.join(mapped)}",
                    proposed_fix="split into separate target columns",
                )
            )
    for missing in submission.missing_required:
        if (DefectType.MISSING_REQUIRED_COLUMN, missing.target_column) not in reported:
            defects.append(
                ProposedDefect(
                    defect_type=DefectType.MISSING_REQUIRED_COLUMN,
                    target_column=missing.target_column,
                    description=missing.reason,
                )
            )
    return reviewed, defects
