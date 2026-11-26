"""Render a run's dbt project from approved mappings, run ``dbt build``, and check the result.

The project lives in ``<run dir>/dbt`` next to the warehouse. For one target table it renders:

- ``int_<table>__checked``: every source row with converted values and a ``reject_reason``
- ``stg_<table>``: the clean rows in the target schema's columns
- ``rejected_<table>``: quarantined rows with the reason and the raw values
- ``schema.yml``: not_null, unique, and accepted_values tests from the target schema

Model SQL is generated from validated transform expressions; it is wrapped in Jinja ``raw`` so
dbt never interprets text inside a transform.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel

from tools.warehouse import ToolError, Warehouse, quote_ident

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "dbt_project" / "templates"
DBT_TIMEOUT_SECONDS = 900
MAX_FAILING_ROWS = 5
# Text that means "no value"; a transform returning NULL for these is not a conversion failure.
NULL_TOKENS = ("null", "none", "nil", "n/a", "na", "#n/a", "nan", "-", "--", "?", "missing")

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    autoescape=False,
)


@dataclass(frozen=True)
class SourceMapping:
    """One approved mapping as the renderer needs it."""

    target_column: str
    source_column: str
    transform_sql: str
    shared_source: bool  # the source column feeds several targets (a merged column)


@dataclass(frozen=True)
class SourceFile:
    file_id: str
    table_name: str  # raw.<table_name>
    raw_columns: list[str]
    mappings: list[SourceMapping]


class NodeResult(BaseModel):
    name: str
    kind: str  # "model" or "test"
    status: str  # success, error, pass, fail, warn, skipped
    failures: int | None = None
    message: str | None = None


class DbtRunResult(BaseModel):
    success: bool
    models_passed: int
    models_failed: int
    tests_passed: int
    tests_failed: int
    elapsed_seconds: float
    results: list[NodeResult]
    error: str | None = None  # set when dbt could not even produce run_results.json


class FailedNode(BaseModel):
    name: str
    kind: str
    status: str
    message: str | None
    failing_rows: list[dict[str, Any]]


class FileAccounting(BaseModel):
    file_id: str
    rows_in: int
    loaded: int
    rejected: int
    reject_reasons: dict[str, int]

    @property
    def balanced(self) -> bool:
        return self.rows_in == self.loaded + self.rejected


def model_names(table: str) -> dict[str, str]:
    return {
        "checked_model": f"int_{table}__checked",
        "staging_model": f"stg_{table}",
        "rejected_model": f"rejected_{table}",
    }


def project_dir(wh: Warehouse) -> Path:
    return wh.path.parent / "dbt"


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _has_input_sql(source_column: str) -> str:
    v = f"trim({quote_ident(source_column)})"
    tokens = ", ".join(_sql_literal(t) for t in NULL_TOKENS)
    return f"(nullif({v}, '') is not null and lower({v}) not in ({tokens}))"


def render_project(
    wh: Warehouse, run_id: str, schema: dict[str, Any], files: list[SourceFile]
) -> Path:
    """Write the dbt project for one target table; returns the project directory."""
    if not files:
        raise ToolError("No approved mappings to build")
    table = schema["table"]
    names = model_names(table)
    key = next((c["name"] for c in schema["columns"] if c.get("unique")), None)
    columns = [
        {
            "name": c["name"],
            "type": c["type"].upper(),
            "required": bool(c.get("required")),
            "allowed_values": c.get("allowed_values", []),
            "tests": [
                *(["not_null"] if c.get("required") else []),
                *(["unique"] if c.get("unique") else []),
                *(["accepted_values"] if c.get("allowed_values") else []),
            ],
        }
        for c in schema["columns"]
    ]

    rendered_files = []
    for f in files:
        by_target = {m.target_column: m for m in f.mappings}
        file_columns = []
        for col in columns:
            m = by_target.get(col["name"])
            if m is not None and "endraw" in m.transform_sql.lower():
                raise ToolError(f"transform for {col['name']} contains a reserved word")
            file_columns.append(
                {
                    "name": col["name"],
                    "type": col["type"],
                    "out_sql": f"({m.transform_sql})" if m else f"cast(null as {col['type']})",
                    # A merged source is non-blank even when the part for this target is empty,
                    # so only single-target sources can show that a value failed to convert.
                    "has_input_sql": (
                        _has_input_sql(m.source_column) if m and not m.shared_source else "false"
                    ),
                }
            )
        raw_json = (
            "json_object("
            + ", ".join(f"{_sql_literal(c)}, {quote_ident(c)}" for c in f.raw_columns)
            + ")::varchar"
        )
        rendered_files.append(
            {
                "file_id": f.file_id,
                "table_name": f.table_name.replace('"', '""'),
                "raw_json": raw_json,
                "columns": file_columns,
            }
        )

    out = project_dir(wh)
    models = out / "models"
    models.mkdir(parents=True, exist_ok=True)
    for stale in models.glob("*"):
        stale.unlink()
    context = {
        "run_id": run_id,
        "warehouse_path": str(wh.path.resolve()),
        "files": rendered_files,
        "columns": columns,
        "key": key,
        "table_description": schema.get("description", table).replace('"', "'"),
        **names,
    }
    outputs = {
        out / "dbt_project.yml": "dbt_project.yml.j2",
        out / "profiles.yml": "profiles.yml.j2",
        models / f"{names['checked_model']}.sql": "checked.sql.j2",
        models / f"{names['staging_model']}.sql": "staging.sql.j2",
        models / f"{names['rejected_model']}.sql": "rejected.sql.j2",
        models / "schema.yml": "schema.yml.j2",
    }
    for path, template in outputs.items():
        path.write_text(_env.get_template(template).render(**context), encoding="utf-8")
    return out


def _dbt_executable() -> str:
    candidate = Path(sys.executable).parent / "dbt"
    if not candidate.exists():
        raise ToolError(f"dbt is not installed next to {sys.executable}")
    return str(candidate)


def run_dbt(wh: Warehouse, select: str | None = None) -> DbtRunResult:
    """Run ``dbt build`` on the rendered project and summarize run_results.json."""
    project = project_dir(wh).resolve()  # dbt runs with cwd=project, so paths must be absolute
    if not (project / "dbt_project.yml").is_file():
        raise ToolError("The dbt project has not been rendered for this run")
    results_path = project / "target" / "run_results.json"
    results_path.unlink(missing_ok=True)
    command = [
        _dbt_executable(), "build", "--project-dir", str(project), "--profiles-dir", str(project),
        "--no-use-colors",
    ]  # fmt: skip
    if select:
        command += ["--select", select]
    env = {**os.environ, "DO_NOT_TRACK": "1", "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=DBT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"dbt build took longer than {DBT_TIMEOUT_SECONDS} seconds") from exc
    elapsed = round(time.monotonic() - started, 2)
    (project / "dbt_build.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")

    if not results_path.is_file():
        tail = "\n".join((completed.stdout + completed.stderr).strip().splitlines()[-15:])
        return DbtRunResult(
            success=False, models_passed=0, models_failed=0, tests_passed=0, tests_failed=0,
            elapsed_seconds=elapsed, results=[], error=tail or "dbt produced no results",
        )  # fmt: skip

    raw = json.loads(results_path.read_text(encoding="utf-8"))
    results = [
        NodeResult(
            name=r["unique_id"].split(".")[2],
            kind=r["unique_id"].split(".")[0],
            status=r["status"],
            failures=r.get("failures"),
            message=r.get("message"),
        )
        for r in raw["results"]
    ]
    counts = Counter((r.kind, r.status in ("success", "pass")) for r in results)
    return DbtRunResult(
        success=completed.returncode == 0 and all(r.status in ("success", "pass") for r in results),
        models_passed=counts[("model", True)],
        models_failed=counts[("model", False)],
        tests_passed=counts[("test", True)],
        tests_failed=counts[("test", False)],
        elapsed_seconds=elapsed,
        results=results,
    )


def get_dbt_errors(wh: Warehouse) -> list[FailedNode]:
    """Error text for every failed node, with up to 5 failing rows for each failed test."""
    results_path = project_dir(wh) / "target" / "run_results.json"
    if not results_path.is_file():
        log = project_dir(wh) / "dbt_build.log"
        if not log.is_file():
            raise ToolError("dbt has not run yet")
        tail = "\n".join(log.read_text(encoding="utf-8").strip().splitlines()[-20:])
        return [
            FailedNode(name="dbt", kind="project", status="error", message=tail, failing_rows=[])
        ]
    failed = []
    raw = json.loads(results_path.read_text(encoding="utf-8"))["results"]
    with wh.connect(read_only=True) as con:
        for r in raw:
            if r["status"] in ("success", "pass", "skipped"):
                continue
            kind = r["unique_id"].split(".")[0]
            rows: list[dict[str, Any]] = []
            relation = r.get("relation_name")
            if kind == "test" and relation:
                try:
                    cursor = con.execute(f"SELECT * FROM {relation} LIMIT {MAX_FAILING_ROWS}")
                    names = [d[0] for d in cursor.description]
                    rows = [
                        {k: (v if isinstance(v, int | float | bool | None) else str(v))
                         for k, v in zip(names, row, strict=True)}
                        for row in cursor.fetchall()
                    ]  # fmt: skip
                except duckdb.Error:
                    rows = []
            failed.append(
                FailedNode(
                    name=r["unique_id"].split(".")[2],
                    kind=kind,
                    status=r["status"],
                    message=r.get("message"),
                    failing_rows=rows,
                )
            )
    return failed


def row_accounting(wh: Warehouse, table: str, file_ids: list[str]) -> list[FileAccounting]:
    """Rows in versus rows loaded plus rows rejected, per file."""
    names = model_names(table)
    with wh.connect(read_only=True) as con:
        rows_in = dict(
            con.execute(
                "SELECT file_id, row_count FROM meta.files WHERE list_contains(?, file_id)",
                [file_ids],
            ).fetchall()
        )
        loaded = dict(
            con.execute(
                f"SELECT _file_id, count(*) FROM main.{names['staging_model']} GROUP BY 1"
            ).fetchall()
        )
        reasons: dict[str, Counter[str]] = {f: Counter() for f in file_ids}
        for file_id, reason, n in con.execute(
            f"SELECT _file_id, reject_reason, count(*) FROM main.{names['rejected_model']} "
            "GROUP BY 1, 2"
        ).fetchall():
            reasons.setdefault(file_id, Counter())[reason] = n
    return [
        FileAccounting(
            file_id=f,
            rows_in=rows_in.get(f, 0),
            loaded=loaded.get(f, 0),
            rejected=sum(reasons[f].values()),
            reject_reasons=dict(reasons[f].most_common()),
        )
        for f in file_ids
    ]
