"""Run phases: Profile (no model) and Map (tool-calling loop). Build arrives in Milestone 4."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent.defects import DEFECT_DESCRIPTIONS
from agent.llm import LLM
from agent.loop import LoopResult, SubmitOutcome, Tool, run_loop
from agent.mapping import (
    MappingSubmission,
    MissingTarget,
    ProposedDefect,
    ReviewedMapping,
    route_for_review,
    validate_submission,
)
from agent.tracing import Tracer
from datagen.schemas import load_schema
from tools.profiling import FileProfile, get_column_profile, profile_file, sample_rows
from tools.transforms import test_transform
from tools.warehouse import FileInfo, ToolError, Warehouse, load_file

PROMPT_DIR = Path(__file__).parent / "prompts"
TOP_VALUES_IN_PROMPT = 5
PATTERNS_IN_PROMPT = 4


# --- Profile phase -------------------------------------------------------------------------


def profile_phase(wh: Warehouse, paths: list[Path]) -> list[tuple[FileInfo, FileProfile]]:
    """Load and profile every file with the profiling functions directly; no model involved."""
    return [(file, profile_file(wh, file.file_id)) for file in (load_file(wh, p) for p in paths)]


# --- Map phase -----------------------------------------------------------------------------


class GetTargetSchemaArgs(BaseModel):
    schema_name: str = Field(description="Target schema name, e.g. customers")


class GetColumnProfileArgs(BaseModel):
    file_id: str
    column: str


class SampleRowsArgs(BaseModel):
    file_id: str
    n: int = Field(default=10, description="Rows to return, at most 20")
    where: str | None = Field(default=None, description="Optional SQL condition over one row")


class TransformTestArgs(BaseModel):
    file_id: str
    sql_expression: str = Field(description="One DuckDB expression over a single row")
    target_type: str = Field(description="Target type, e.g. date, decimal(10,2), boolean")


@dataclass
class MapOutcome:
    status: str  # "submitted" or "failed"
    reason: str | None
    mappings: list[ReviewedMapping] = field(default_factory=list)
    missing_required: list[MissingTarget] = field(default_factory=list)
    defects: list[ProposedDefect] = field(default_factory=list)
    loop: LoopResult | None = None


def map_prompt(threshold: float) -> str:
    template = (PROMPT_DIR / "map.md").read_text(encoding="utf-8")
    defect_lines = "\n".join(f"- `{t}`: {d}" for t, d in DEFECT_DESCRIPTIONS.items())
    return template.replace("{defect_types}", defect_lines).replace(
        "{threshold}", f"{threshold:.2f}"
    )


def describe_task(file: FileInfo, profile: FileProfile, schema: dict[str, Any]) -> str:
    """The first user message: compact source profiles plus the target schema."""
    columns = []
    for c in profile.columns:
        columns.append(
            {
                "column": c.column_name,
                "inferred_type": c.inferred_type,
                "type_match_rate": c.type_match_rate,
                "null_count": c.null_count,
                "null_like_count": c.null_like_count,
                "distinct_count": c.distinct_count,
                "top_values": [t.value[:60] for t in c.top_values[:TOP_VALUES_IN_PROMPT]],
                "patterns": dict(list(c.patterns.items())[:PATTERNS_IN_PROMPT]),
            }
        )
    return (
        f"Map file {file.filename!r} (file_id {file.file_id!r}, {file.row_count} rows) onto the "
        f"target schema {schema['table']!r}.\n\n"
        f"Source columns:\n{json.dumps(columns, indent=1)}\n\n"
        f"Target schema:\n{json.dumps(schema, indent=1)}"
    )


def map_tools(
    wh: Warehouse,
    schema: dict[str, Any],
    source_columns: list[str],
    on_accept: Callable[[MappingSubmission], None],
) -> list[Tool]:
    def submit(args: MappingSubmission) -> SubmitOutcome:
        normalized, errors = validate_submission(args, schema, source_columns)
        if errors:
            return SubmitOutcome(accepted=False, errors=errors)
        on_accept(normalized)
        return SubmitOutcome(accepted=True, value=normalized)

    def target_schema(args: GetTargetSchemaArgs) -> dict[str, Any]:
        try:
            return load_schema(args.schema_name)
        except FileNotFoundError as exc:
            raise ToolError(str(exc)) from exc

    return [
        Tool(
            "get_target_schema",
            "Return a target schema: columns with type, required, unique, allowed values, and "
            "description.",
            GetTargetSchemaArgs,
            target_schema,
        ),
        Tool(
            "get_column_profile",
            "Return the stored profile of one source column: type, nulls, distinct count, "
            "min/max, top values, and value patterns.",
            GetColumnProfileArgs,
            lambda a: get_column_profile(wh, a.file_id, a.column),
        ),
        Tool(
            "sample_rows",
            "Return up to 20 raw rows, optionally filtered by a SQL condition over one row.",
            SampleRowsArgs,
            lambda a: sample_rows(wh, a.file_id, a.n, a.where),
        ),
        Tool(
            "test_transform",
            "Run a transform expression over every row and report the share that converts to "
            "the target type, with up to 5 failing examples.",
            TransformTestArgs,
            lambda a: test_transform(wh, a.file_id, a.sql_expression, a.target_type),
        ),
        Tool(
            "submit_mappings",
            "Submit all mappings, missing required targets, and defects. Returns validation "
            "errors to fix, or 'Accepted.'",
            MappingSubmission,
            submit,
            submits=True,
        ),
    ]


def map_phase(
    llm: LLM,
    wh: Warehouse,
    file: FileInfo,
    profile: FileProfile,
    schema: dict[str, Any],
    *,
    threshold: float,
    max_steps: int,
    tracer: Tracer,
    retries: int = 3,
    backoff_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> MapOutcome:
    accepted: list[MappingSubmission] = []
    columns = [c.column_name for c in profile.columns]
    loop = run_loop(
        llm,
        map_prompt(threshold),
        describe_task(file, profile, schema),
        map_tools(wh, schema, columns, accepted.append),
        max_steps=max_steps,
        tracer=tracer,
        retries=retries,
        backoff_seconds=backoff_seconds,
        sleep=sleep,
    )
    if loop.status != "submitted":
        return MapOutcome(status="failed", reason=loop.reason, loop=loop)
    submission = accepted[-1]
    mappings, defects = route_for_review(submission, threshold)
    return MapOutcome(
        status="submitted",
        reason=None,
        mappings=mappings,
        missing_required=submission.missing_required,
        defects=defects,
        loop=loop,
    )
