"""Mapping contracts: a finished run's approved mappings saved as a versioned source.

A contract version keeps the mappings, a fingerprint of the file's columns, and a baseline
profile that later files are compared with (``agent.drift``). Saving a contract from an
onboarding run also loads that run's clean rows into the source's table; every recurring load
adds its own rows there and saves a new version when its mappings or columns changed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.drift import RECENT_LOADS, column_baseline
from agent.phases import BUILDABLE
from agent.settings import Settings
from api.models import MappingContract, Run, RunStatus, RunType, SourceFile
from datagen.schemas import load_schema
from tools.loads import LoadResult, source_db_path, upsert_rows
from tools.profiling import ColumnProfile as ToolColumnProfile
from tools.profiling import FileProfile
from tools.transforms import check_values
from tools.warehouse import ToolError, Warehouse


def fingerprint(columns: list[tuple[str, str]]) -> str:
    """sha256 of the sorted ``name:inferred_type`` pairs of a file's columns."""
    text = "\n".join(sorted(f"{name}:{kind}" for name, kind in columns))
    return hashlib.sha256(text.encode()).hexdigest()


def _file_profile(source: SourceFile) -> FileProfile:
    return FileProfile(
        file_id=source.warehouse_file_id,
        filename=source.filename,
        row_count=source.row_count,
        columns=[ToolColumnProfile.model_validate(p.profile) for p in source.profiles],
    )


def run_mappings(run: Run) -> list[dict[str, Any]]:
    """The mappings a contract keeps: every approved or edited mapping with a source column."""
    return [
        {
            "source_column": m.source_column,
            "target_column": m.target_column,
            "transform_sql": m.transform_sql,
        }
        for m in run.mappings
        if m.status in BUILDABLE and m.source_column
    ]


def baseline_profile(
    wh: Warehouse, profile: FileProfile, schema: dict[str, Any], mappings: list[dict[str, Any]]
) -> dict[str, Any]:
    """Per-column profile summaries plus how each saved transform does on this file."""
    targets = {c["name"]: c for c in schema["columns"]}
    checks = {}
    for m in mappings:
        spec = targets[m["target_column"]]
        result = check_values(
            wh, profile.file_id, m["transform_sql"], m["source_column"], spec["type"],
            spec.get("allowed_values"),
        )  # fmt: skip
        checks[m["target_column"]] = {
            "source_column": m["source_column"],
            "failure_rate": result.failure_rate,
            "failing_values": result.failing_values,
        }
    return {
        "row_count": profile.row_count,
        "columns": {c.column_name: column_baseline(c) for c in profile.columns},
        "checks": checks,
    }


def latest_version(
    session: Session, customer_name: str, source_name: str
) -> MappingContract | None:
    return session.scalars(
        select(MappingContract)
        .where(
            MappingContract.customer_name == customer_name,
            MappingContract.source_name == source_name,
        )
        .order_by(MappingContract.version.desc())
        .limit(1)
    ).first()


def versions(session: Session, contract: MappingContract) -> list[MappingContract]:
    """Every version of a contract's source, newest first."""
    return list(
        session.scalars(
            select(MappingContract)
            .where(
                MappingContract.customer_name == contract.customer_name,
                MappingContract.source_name == contract.source_name,
            )
            .order_by(MappingContract.version.desc())
        )
    )


def source_loads(
    session: Session, contract: MappingContract, limit: int | None = None
) -> list[Run]:
    """Recurring loads of a contract's source (any version), newest first."""
    ids = [v.id for v in versions(session, contract)]
    query = (
        select(Run)
        .where(Run.run_type == RunType.RECURRING, Run.contract_id.in_(ids))
        .order_by(Run.created_at.desc())
    )
    if limit:
        query = query.limit(limit)
    return list(session.scalars(query))


def recent_row_counts(session: Session, contract: MappingContract) -> list[int]:
    """Rows of the last few finished loads of the source, plus the contract's baseline file."""
    done = [r for r in source_loads(session, contract) if r.status == RunStatus.DONE]
    counts = [sum(f.row_count for f in r.files) for r in done[:RECENT_LOADS]]
    return [*counts, contract.baseline_profile["row_count"]]


def source_table_path(settings: Settings, contract: MappingContract) -> Path:
    return source_db_path(settings.data_dir, contract.customer_name, contract.source_name)


def new_version(
    session: Session, settings: Settings, run: Run, source_name: str
) -> MappingContract:
    """Save the run's approved mappings as the next version of (customer, source_name)."""
    if len(run.files) != 1:
        raise ToolError("A recurring source is saved from a run with exactly one file")
    [source] = run.files
    schema = load_schema(run.target_schema)
    mappings = run_mappings(run)
    if not mappings:
        raise ToolError(f"Run {run.id} has no approved mappings to save")
    previous = latest_version(session, run.customer_name, source_name)
    wh = Warehouse.for_run(run.id, settings.data_dir)
    profile = _file_profile(source)
    contract = MappingContract(
        customer_name=run.customer_name,
        source_name=source_name,
        version=(previous.version + 1) if previous else 1,
        target_schema=run.target_schema,
        fingerprint=fingerprint([(c.column_name, c.inferred_type) for c in profile.columns]),
        baseline_profile=baseline_profile(wh, profile, schema, mappings),
        mappings=mappings,
        created_from_run_id=run.id,
    )
    session.add(contract)
    session.commit()
    return contract


def load_into_source(
    session: Session, settings: Settings, run: Run, contract: MappingContract
) -> LoadResult:
    """Upsert a built run's clean rows into the source's table; recorded in the build report."""
    result = upsert_rows(
        source_table_path(settings, contract),
        Warehouse.for_run(run.id, settings.data_dir),
        load_schema(run.target_schema),
        run.id,
    )
    run.build_report = {**(run.build_report or {}), "load": result.model_dump()}
    session.commit()
    return result


def saved_contract_id(session: Session, run: Run) -> int | None:
    """The contract version saved from this run, if any."""
    return session.scalar(
        select(MappingContract.id).where(MappingContract.created_from_run_id == run.id).limit(1)
    )


def save_contract(
    session: Session, settings: Settings, run: Run, source_name: str
) -> MappingContract:
    """Save a finished onboarding run as a recurring source and load its rows into the table."""
    if run.run_type != RunType.ONBOARDING or run.status != RunStatus.DONE:
        raise ToolError(
            f"Run {run.id} is {run.status}; only a finished onboarding run can be saved as a source"
        )
    if saved_contract_id(session, run) is not None:
        raise ToolError(f"Run {run.id} is already saved as a recurring source")
    contract = new_version(session, settings, run, source_name)
    load_into_source(session, settings, run, contract)
    return contract


def changed_since(run: Run, contract: MappingContract) -> bool:
    """Whether a recurring load's mappings or columns differ from the contract it was checked
    against, so the next load should be compared with a new version."""
    [source] = run.files
    columns = [(c.column_name, c.inferred_type) for c in _file_profile(source).columns]

    def triples(mappings: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
        return sorted(
            (m["source_column"], m["target_column"], m["transform_sql"]) for m in mappings
        )

    return (
        triples(contract.mappings) != triples(run_mappings(run))
        or fingerprint(columns) != contract.fingerprint
    )
