"""Score one run against its answer key, and aggregate scores into scorecard metrics."""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import duckdb

from agent.defects import DefectType
from api.models import Run, RunStatus
from datagen.schemas import key_column, load_schema
from tools.dbt_runner import model_names
from tools.warehouse import ToolError, Warehouse


@dataclass
class CaseScore:
    case_id: str
    table: str
    names_changed: bool
    corruptions: list[str]
    status: str
    error: str | None
    repeat: int = 1
    source_columns: int = 0
    mapping_correct: int = 0
    mappings_total: int = 0  # proposed mappings plus missing-target rows
    escalated: int = 0
    bad_auto_approvals: int = 0
    defects_injected: int = 0
    defects_recalled: int = 0
    defects_flagged: int = 0
    defects_flagged_correct: int = 0
    build_passed: bool = False
    repairs: int = 0
    rows_loaded: int = 0
    rows_rejected: int = 0
    cells_total: int = 0
    cells_correct: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    tokens: int = 0
    duration_seconds: float = 0.0
    wrong_mappings: list[dict[str, Any]] = field(default_factory=list)
    missed_defects: list[str] = field(default_factory=list)
    spurious_defects: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """A run passes when every column is mapped correctly and the build passed."""
        return self.build_passed and self.mapping_correct == self.source_columns


def defect_matches(injected: dict[str, Any], flagged: Any) -> bool:
    """Same type and same column. Duplicates are a table-level defect: the type is enough."""
    if flagged.defect_type != injected["type"]:
        return False
    if injected["type"] == DefectType.DUPLICATE_ROWS:
        return True
    if injected["source_column"] is not None and flagged.column_name == injected["source_column"]:
        return True
    return flagged.target_column is not None and flagged.target_column in injected["target_columns"]


def score_case(key: dict[str, Any], run: Run, threshold: float) -> CaseScore:
    truth: dict[str, set[str]] = {}
    for m in key["mappings"]:
        truth.setdefault(m["source_column"], set()).add(m["target_column"])
    for source in key["unmapped_source_columns"]:
        truth[source] = set()
    true_pairs = {(m["source_column"], m["target_column"]) for m in key["mappings"]}

    predicted: dict[str, set[str]] = {}
    for m in run.mappings:
        if m.source_column is not None:
            predicted.setdefault(m.source_column, set()).add(m.target_column)

    report = run.build_report or {}
    accounting = report.get("row_accounting", [])
    score = CaseScore(
        case_id=key["case_id"],
        table=key["table"],
        names_changed=key["names_changed"],
        corruptions=key["corruptions"],
        status=run.status,
        error=run.error,
        source_columns=len(truth),
        mappings_total=len(run.mappings),
        escalated=sum(1 for m in run.mappings if m.review_reason is not None),
        build_passed=run.status == RunStatus.DONE,
        repairs=report.get("repairs", 0),
        rows_loaded=sum(a["loaded"] for a in accounting),
        rows_rejected=sum(a["rejected"] for a in accounting),
        model_calls=run.model_calls,
        tool_calls=run.tool_calls,
        tokens=run.prompt_tokens + run.completion_tokens,
        duration_seconds=run.duration_seconds or 0.0,
    )
    wrong_and_auto = {
        m.source_column: m.confidence
        for m in run.mappings
        if m.confidence is not None
        and m.confidence >= threshold
        and (m.source_column, m.target_column) not in true_pairs
    }
    score.bad_auto_approvals = sum(
        1
        for m in run.mappings
        if m.confidence is not None
        and m.confidence >= threshold
        and (m.source_column, m.target_column) not in true_pairs
    )
    for source, targets in truth.items():
        got = predicted.get(source, set())
        if got == targets:
            score.mapping_correct += 1
            continue
        wrong: dict[str, Any] = {
            "source": source,
            "predicted": sorted(got),
            "truth": sorted(targets),
        }
        if source in wrong_and_auto:
            wrong["auto_approved_confidence"] = wrong_and_auto[source]
        score.wrong_mappings.append(wrong)

    injected = key["defects"]
    flagged = list(run.defects)
    score.defects_injected = len(injected)
    score.defects_flagged = len(flagged)
    for d in injected:
        if any(defect_matches(d, f) for f in flagged):
            score.defects_recalled += 1
        else:
            score.missed_defects.append(f"{d['type']}@{d['source_column'] or d['target_columns']}")
    for f in flagged:
        if any(defect_matches(d, f) for d in injected):
            score.defects_flagged_correct += 1
        else:
            score.spurious_defects.append(
                f"{f.defect_type}@{f.column_name or f.target_column or '-'}"
            )
    return score


# --- Cell accuracy ---------------------------------------------------------------------------


def normalize_cell(value: Any, column_type: str) -> str:
    """Render a clean-table string or a loaded value the same way, so they compare equal."""
    if value is None or value == "":
        return ""
    if column_type == "date":
        return value.isoformat() if isinstance(value, date | datetime) else str(value)[:10]
    if column_type.startswith("decimal"):
        try:
            return str(Decimal(str(value)).quantize(Decimal("0.01")))
        except InvalidOperation:
            return str(value)
    if column_type == "integer":
        try:
            return str(int(Decimal(str(value))))
        except InvalidOperation:
            return str(value)
    if column_type == "boolean":
        return (
            "true"
            if value in (True, "true")
            else "false"
            if value in (False, "false")
            else str(value)
        )
    return str(value)


def score_cells(score: CaseScore, key: dict[str, Any], case_dir: Path, wh: Warehouse) -> None:
    """Cells of the loaded table equal to the clean table, joined on the key.

    Every clean row counts, so rows that were quarantined or never loaded count as wrong.
    """
    schema = load_schema(key["table"])
    types = {c["name"]: c["type"] for c in schema["columns"]}
    key_name = key_column(schema)
    with (case_dir / key["clean_file"]).open(encoding="utf-8", newline="") as fh:
        clean = list(csv.DictReader(fh))
    score.cells_total = len(clean) * len(types)

    loaded: dict[str, dict[str, Any]] = {}
    try:
        with wh.connect(read_only=True) as con:
            cursor = con.execute(f"SELECT * FROM main.{model_names(key['table'])['staging_model']}")
            names = [d[0] for d in cursor.description]
            for row in cursor.fetchall():
                record = dict(zip(names, row, strict=True))
                loaded.setdefault(str(record[key_name]), record)
    except (duckdb.Error, ToolError):
        return  # nothing was loaded; every cell counts as wrong

    correct = 0
    for row in clean:
        out = loaded.get(row[key_name])
        if out is None:
            continue
        correct += sum(
            normalize_cell(out.get(col), t) == normalize_cell(row[col], t)
            for col, t in types.items()
        )
    score.cells_correct = correct


# --- Aggregation ------------------------------------------------------------------------------


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def aggregate(scores: list[CaseScore]) -> dict[str, Any]:
    """Micro-averaged metrics: totals over all runs, not an average of per-case ratios."""

    def total(attr: str, subset: list[CaseScore] | None = None) -> float:
        return sum(getattr(s, attr) for s in (scores if subset is None else subset))

    renamed = [s for s in scores if s.names_changed]
    kept = [s for s in scores if not s.names_changed]
    by_case: dict[str, list[CaseScore]] = defaultdict(list)
    for s in scores:
        by_case[s.case_id].append(s)
    repeated = {case: runs for case, runs in by_case.items() if len(runs) > 1}
    return {
        "cases": len(by_case),
        "runs": len(scores),
        "failed_runs": sum(1 for s in scores if s.status == RunStatus.FAILED),
        "mapping_accuracy": _ratio(total("mapping_correct"), total("source_columns")),
        "mapping_accuracy_names_changed": _ratio(
            total("mapping_correct", renamed), total("source_columns", renamed)
        ),
        "mapping_accuracy_names_unchanged": _ratio(
            total("mapping_correct", kept), total("source_columns", kept)
        ),
        "defect_recall": _ratio(total("defects_recalled"), total("defects_injected")),
        "defect_precision": _ratio(total("defects_flagged_correct"), total("defects_flagged")),
        "cell_accuracy": _ratio(total("cells_correct"), total("cells_total")),
        "escalation_rate": _ratio(total("escalated"), total("mappings_total")),
        "bad_auto_approvals": int(total("bad_auto_approvals")),
        "build_success": _ratio(sum(s.build_passed for s in scores), len(scores)),
        "consistency": _ratio(
            sum(all(r.passed for r in runs) for runs in repeated.values()), len(repeated)
        ),
        "repairs": int(total("repairs")),
        "model_calls": int(total("model_calls")),
        "tokens": int(total("tokens")),
        "duration_seconds": round(total("duration_seconds"), 1),
    }
