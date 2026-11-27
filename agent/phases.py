"""Run phases: Map (tool-calling loop) and Build (dbt, with repairs).

Profiling needs no model; the runner calls the profiling tool for each file directly.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

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
from agent.review import mapping_state, source_columns, validated_sql
from agent.tool_backend import RunTools
from agent.tracing import Tracer
from api.models import MappingStatus, ReviewEvent, Run
from datagen.schemas import load_schema
from tools.dbt_runner import (
    DbtRunResult,
    FileAccounting,
    SourceFile,
    SourceMapping,
    render_project,
    row_accounting,
)
from tools.profiling import FileProfile
from tools.warehouse import FileInfo, ToolError, Warehouse

PROMPT_DIR = Path(__file__).parent / "prompts"
TOP_VALUES_IN_PROMPT = 5
PATTERNS_IN_PROMPT = 4


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
    tools: RunTools,
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
            lambda a: tools.get_column_profile(a.file_id, a.column),
        ),
        Tool(
            "sample_rows",
            "Return up to 20 raw rows, optionally filtered by a SQL condition over one row.",
            SampleRowsArgs,
            lambda a: tools.sample_rows(a.file_id, a.n, a.where),
        ),
        Tool(
            "test_transform",
            "Run a transform expression over every row and report the share that converts to "
            "the target type, with up to 5 failing examples.",
            TransformTestArgs,
            lambda a: tools.test_transform(a.file_id, a.sql_expression, a.target_type),
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
    tools: RunTools,
    file: FileInfo,
    profile: FileProfile,
    schema: dict[str, Any],
    *,
    threshold: float,
    max_steps: int,
    tracer: Tracer,
    retries: int = 3,
    backoff_seconds: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> MapOutcome:
    accepted: list[MappingSubmission] = []
    columns = [c.column_name for c in profile.columns]
    loop = run_loop(
        llm,
        map_prompt(threshold),
        describe_task(file, profile, schema),
        map_tools(tools, schema, columns, accepted.append),
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


# --- Build phase ---------------------------------------------------------------------------

BUILDABLE = (MappingStatus.APPROVED, MappingStatus.EDITED)


class UpdateTransformArgs(BaseModel):
    mapping_id: int
    transform_sql: str = Field(description="New DuckDB expression over a single source row")
    reason: str = Field(min_length=1, description="What was wrong and how this fixes it")


class RunDbtArgs(BaseModel):
    select: str | None = Field(default=None, description="Optional dbt node selection")


class NoArgs(BaseModel):
    pass


class SubmitBuildArgs(BaseModel):
    summary: str = Field(min_length=1, description="What was repaired and what still fails")


@dataclass
class BuildOutcome:
    status: str  # "passed" or "failed"
    reason: str | None
    result: DbtRunResult
    accounting: list[FileAccounting] = field(default_factory=list)
    repairs: int = 0  # rebuilds run by the repair agent
    loop: LoopResult | None = None


def build_sources(run: Run) -> list[SourceFile]:
    """Approved and edited mappings, grouped by source file, as the dbt renderer needs them."""
    sources = []
    for source in run.files:
        mappings = [
            m
            for m in run.mappings
            if m.file_id == source.id and m.status in BUILDABLE and m.source_column
        ]
        per_source = Counter(m.source_column for m in mappings)
        sources.append(
            SourceFile(
                file_id=source.warehouse_file_id,
                table_name=source.raw_table,
                raw_columns=source_columns(source),
                mappings=[
                    SourceMapping(
                        m.target_column,
                        m.source_column,
                        m.transform_sql,
                        per_source[m.source_column] > 1,
                    )
                    for m in mappings
                ],
            )
        )
    return sources


def _failure_summary(result: DbtRunResult) -> str:
    if result.error:
        return result.error
    failed = [r for r in result.results if r.status not in ("success", "pass")]
    return "; ".join(f"{r.kind} {r.name}: {r.status} ({r.message})" for r in failed[:5])


def describe_build_failure(run: Run, schema: dict[str, Any], tools: RunTools) -> str:
    types = {c["name"]: c["type"] for c in schema["columns"]}
    files = {f.id: f for f in run.files}
    mappings = [
        {
            "mapping_id": m.id,
            "file_id": files[m.file_id].warehouse_file_id,
            "source_column": m.source_column,
            "target_column": m.target_column,
            "target_type": types.get(m.target_column),
            "transform_sql": m.transform_sql,
        }
        for m in run.mappings
        if m.status in BUILDABLE and m.source_column
    ]
    errors = [e.model_dump() for e in tools.get_dbt_errors()]
    return (
        f"dbt build failed for target table {schema['table']!r}.\n\n"
        f"Mappings:\n{json.dumps(mappings, indent=1)}\n\n"
        f"Failures:\n{json.dumps(errors, indent=1, default=str)}"
    )


def build_phase(
    session: Session,
    run: Run,
    wh: Warehouse,
    tools: RunTools,
    llm: LLM | None,
    schema: dict[str, Any],
    *,
    max_repairs: int,
    max_steps: int,
    tracer: Tracer,
    retries: int = 3,
    backoff_seconds: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> BuildOutcome:
    """Render and run dbt; on failure let the model repair transforms up to ``max_repairs``
    rebuilds; then check row accounting."""

    def build(select: str | None = None) -> DbtRunResult:
        render_project(wh, run.id, schema, build_sources(run))
        with tracer.span("dbt:build", "TOOL", inputs={"select": select}) as span:
            result = tools.run_dbt(select)
            span.set_outputs(result.model_dump(exclude={"results"}))
        return result

    state = {"result": build(), "repairs": 0, "selected": False}
    loop = None
    if not state["result"].success and llm is not None and max_repairs > 0:
        buildable = {m.id: m for m in run.mappings if m.status in BUILDABLE and m.source_column}
        files = {f.id: f for f in run.files}

        def update_transform(args: UpdateTransformArgs) -> dict[str, Any]:
            mapping = buildable.get(args.mapping_id)
            if mapping is None:
                raise ToolError(
                    f"Unknown mapping_id {args.mapping_id}. Buildable mappings: "
                    f"{', '.join(str(i) for i in buildable)}"
                )
            sql = validated_sql(files[mapping.file_id], args.transform_sql)
            before = mapping_state(mapping)
            mapping.transform_sql = sql
            session.add(
                ReviewEvent(
                    run_id=run.id,
                    item_type="mapping",
                    item_id=mapping.id,
                    action="agent_repair",
                    before=before,
                    after={**mapping_state(mapping), "actor": "agent", "reason": args.reason},
                )
            )
            session.commit()
            return {"mapping_id": mapping.id, "transform_sql": sql, "updated": True}

        def rebuild(args: RunDbtArgs) -> SubmitOutcome:
            state["result"] = build(args.select)
            state["repairs"] += 1
            state["selected"] = bool(args.select)
            result = state["result"]
            left = max_repairs - state["repairs"]
            summary = result.model_dump_json(exclude={"results"})
            if result.success:
                return SubmitOutcome(True, message=f"Build passed. {summary}")
            if left <= 0:
                message = f"Build failed and no rebuilds are left. {summary}"
                return SubmitOutcome(True, message=message)
            return SubmitOutcome(
                False,
                message=f"Build failed ({left} rebuilds left): {_failure_summary(result)}. "
                "Call get_dbt_errors for the failing rows.",
            )

        loop = run_loop(
            llm,
            (PROMPT_DIR / "build.md")
            .read_text(encoding="utf-8")
            .replace("{max_repairs}", str(max_repairs)),
            describe_build_failure(run, schema, tools),
            [
                Tool(
                    "update_transform",
                    "Replace one mapping's transform_sql (validated before it is saved).",
                    UpdateTransformArgs,
                    update_transform,
                ),
                Tool(
                    "run_dbt",
                    "Re-render the dbt project and run dbt build; returns pass and fail counts.",
                    RunDbtArgs,
                    rebuild,
                    submits=True,
                ),
                Tool(
                    "get_dbt_errors",
                    "Error text and up to 5 failing rows for every failed model and test.",
                    NoArgs,
                    lambda _: tools.get_dbt_errors(),
                ),
                Tool(
                    "test_transform",
                    "Run an expression over every row and report the share that converts.",
                    TransformTestArgs,
                    lambda a: tools.test_transform(a.file_id, a.sql_expression, a.target_type),
                ),
                Tool(
                    "submit_build",
                    "Stop repairing and report what is still wrong.",
                    SubmitBuildArgs,
                    lambda a: SubmitOutcome(True, value=a.summary),
                    submits=True,
                ),
            ],
            max_steps=max_steps,
            tracer=tracer,
            retries=retries,
            backoff_seconds=backoff_seconds,
            sleep=sleep,
        )
        if state["selected"] and state["result"].success:
            state["result"] = build()  # a partial selection passing is not a passing build

    result: DbtRunResult = state["result"]
    outcome = BuildOutcome(
        status="failed", reason=None, result=result, repairs=state["repairs"], loop=loop
    )
    if not result.success:
        outcome.reason = f"dbt build failed: {_failure_summary(result)}"
        if loop is not None and loop.status != "submitted":
            outcome.reason += f" (repair stopped: {loop.reason})"
        return outcome
    file_ids = [f.warehouse_file_id for f in run.files]
    outcome.accounting = row_accounting(wh, schema["table"], file_ids)
    unbalanced = [a for a in outcome.accounting if not a.balanced]
    if unbalanced:
        outcome.reason = "Row accounting failed: " + "; ".join(
            f"{a.file_id}: {a.rows_in} in, {a.loaded} loaded, {a.rejected} rejected"
            for a in unbalanced
        )
        return outcome
    outcome.status = "passed"
    return outcome
