"""Drift checks: compare a new file from a saved source with the source's contract.

Everything here is deterministic and makes no model call. Each finding says what changed and
what happens next (spec §13, Milestone 7):

| Check | Trigger | Action |
| --- | --- | --- |
| column_added | a column the contract does not know | propose: the agent maps it, a person reviews |
| column_missing | a contract column is absent | block if it fed a required target, else warn |
| column_renamed | one missing and one added column with similar values | propose: reuse the saved mapping, a person confirms |
| format_change | the saved transform fails on more values than at the baseline | propose: the agent writes a new transform, a person reviews |
| new_category | values the saved transform does not turn into an allowed value | propose: the agent maps them, a person reviews; unknown values are quarantined |
| null_rate_jump | empty values more than 10 points above the baseline | warn |
| volume_anomaly | rows below 50% or above 200% of the recent average | warn |
"""  # noqa: E501

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from api.models import MappingContract
from tools.profiling import ColumnProfile, FileProfile
from tools.transforms import check_values
from tools.warehouse import Warehouse

Check = Literal[
    "column_added",
    "column_missing",
    "column_renamed",
    "format_change",
    "new_category",
    "null_rate_jump",
    "volume_anomaly",
]
Action = Literal["propose", "block", "warn"]

EMPTY_RATE_JUMP = 0.10  # empty values more than 10 points above the baseline
# ...and a rise of at least 3 standard errors, so small files do not raise alarms by chance
EMPTY_RATE_Z = 3.0
FAILURE_RATE_JUMP = 0.02  # the saved transform fails on 2 points more of the real values
VOLUME_LOW, VOLUME_HIGH = 0.5, 2.0  # rows against the recent average
RENAME_SIMILARITY = 0.5  # share of value shapes or top values two columns must have in common
RECENT_LOADS = 5
_IGNORED_PATTERNS = {"other shapes", "null-like text"}


class Finding(BaseModel):
    check: Check
    source_column: str | None
    target_column: str | None
    action: Action
    message: str  # plain language, shown in the web app
    details: dict[str, Any] = {}


def empty_rate(column: ColumnProfile) -> float:
    """Share of rows that are blank or hold a null token such as "N/A"."""
    if not column.row_count:
        return 0.0
    return round((column.null_count + column.null_like_count) / column.row_count, 4)


def column_baseline(column: ColumnProfile) -> dict[str, Any]:
    """What the contract keeps about one source column."""
    return {
        "inferred_type": column.inferred_type,
        "empty_rate": empty_rate(column),
        "patterns": column.patterns,
        "top_values": [t.value for t in column.top_values],
    }


def similarity(baseline: dict[str, Any], column: ColumnProfile) -> float:
    """How alike an old and a new column look: 0 when their types differ, else the larger of
    the share of value shapes and the share of top values they have in common."""
    if baseline["inferred_type"] != column.inferred_type:
        return 0.0
    old_shapes = set(baseline["patterns"]) - _IGNORED_PATTERNS
    new_shapes = set(column.patterns) - _IGNORED_PATTERNS
    shapes = len(old_shapes & new_shapes) / len(old_shapes | new_shapes) if old_shapes else 0.0
    old_top, new_top = set(baseline["top_values"]), {t.value for t in column.top_values}
    smaller = min(len(old_top), len(new_top))
    top = len(old_top & new_top) / smaller if smaller else 0.0
    return round(max(shapes, top), 2)


def _quote(names: list[str]) -> str:
    return ", ".join(f"'{n}'" for n in names)


def _pct(rate: float) -> str:
    return f"{round(rate * 100)}%"


def _significant_rise(before: float, n_before: int, now: float, n_now: int) -> bool:
    """Two-proportion z-test: is ``now`` higher than ``before`` beyond sampling noise?"""
    if not n_before or not n_now:
        return True
    pooled = (before * n_before + now * n_now) / (n_before + n_now)
    error = (pooled * (1 - pooled) * (1 / n_before + 1 / n_now)) ** 0.5
    return error == 0 or (now - before) / error >= EMPTY_RATE_Z


def detect(
    contract: MappingContract,
    profile: FileProfile,
    wh: Warehouse,
    schema: dict[str, Any],
    recent_row_counts: list[int],
) -> list[Finding]:
    """Every difference between a new file and its source's contract; blocks first."""
    baseline = contract.baseline_profile
    old_columns: dict[str, dict[str, Any]] = baseline["columns"]
    new_columns = {c.column_name: c for c in profile.columns}
    targets = {c["name"]: c for c in schema["columns"]}
    mappings_by_source: dict[str, list[dict[str, Any]]] = {}
    for m in contract.mappings:
        mappings_by_source.setdefault(m["source_column"], []).append(m)
    findings: list[Finding] = []

    added = [c for c in new_columns if c not in old_columns]
    missing = [c for c in old_columns if c not in new_columns]

    # A missing column and an added column that look alike are one renamed column.
    for old in [m for m in missing if m in mappings_by_source]:
        scored = sorted(
            ((similarity(old_columns[old], new_columns[new]), new) for new in added),
            reverse=True,
        )
        if not scored or scored[0][0] < RENAME_SIMILARITY:
            continue
        score, new = scored[0]
        added.remove(new)
        missing.remove(old)
        mapped = [m["target_column"] for m in mappings_by_source[old]]
        findings.append(
            Finding(
                check="column_renamed",
                source_column=new,
                target_column=mapped[0],
                action="propose",
                message=(
                    f"Column '{old}' seems to be renamed to '{new}' ({_pct(score)} of its values "
                    f"look alike). The saved mapping to {_quote(mapped)} is reused for '{new}'; "
                    "please confirm."
                ),
                details={"old": old, "new": new, "similarity": score, "targets": mapped},
            )
        )

    for old in missing:
        mapped = [m["target_column"] for m in mappings_by_source.get(old, [])]
        required = [t for t in mapped if targets.get(t, {}).get("required")]
        if required:
            message = (
                f"Column '{old}' is missing. It fills the required column {_quote(required)}, "
                "so the load is blocked. Fix the file, or onboard it as a new source."
            )
            action: Action = "block"
        elif mapped:
            message = f"Column '{old}' is missing, so {_quote(mapped)} will be empty in this load."
            action = "warn"
        else:
            message = f"Column '{old}' is missing. It was not loaded before either."
            action = "warn"
        findings.append(
            Finding(
                check="column_missing",
                source_column=old,
                target_column=mapped[0] if mapped else None,
                action=action,
                message=message,
                details={"targets": mapped},
            )
        )

    for new in added:
        findings.append(
            Finding(
                check="column_added",
                source_column=new,
                target_column=None,
                action="propose",
                message=(
                    f"New column '{new}' is not in the saved source. The agent proposes where it "
                    "goes, or that it stays out."
                ),
                details={"inferred_type": new_columns[new].inferred_type},
            )
        )

    checks = baseline.get("checks", {})
    for m in contract.mappings:
        source, target = m["source_column"], m["target_column"]
        if source not in new_columns or target not in checks:
            continue
        spec = targets[target]
        allowed = spec.get("allowed_values")
        before = checks[target]
        now = check_values(wh, profile.file_id, m["transform_sql"], source, spec["type"], allowed)
        if allowed:
            unknown = {v: n for v, n in now.failing_values.items()
                       if v not in before["failing_values"]}  # fmt: skip
            if unknown:
                listed = ", ".join(f"'{v}' ({n} rows)" for v, n in list(unknown.items())[:5])
                findings.append(
                    Finding(
                        check="new_category",
                        source_column=source,
                        target_column=target,
                        action="propose",
                        message=(
                            f"'{source}' has values the saved mapping for '{target}' does not "
                            f"know: {listed}. The agent proposes how to map them; rows that stay "
                            "unknown are set aside."
                        ),
                        details={"values": unknown, "transform_sql": m["transform_sql"]},
                    )
                )
        elif now.failure_rate - before["failure_rate"] > FAILURE_RATE_JUMP:
            old_shapes = set(old_columns.get(source, {}).get("patterns", {}))
            new_shapes = [p for p in new_columns[source].patterns
                          if p not in old_shapes and p not in _IGNORED_PATTERNS]  # fmt: skip
            shapes = f" New value shapes: {', '.join(new_shapes[:4])}." if new_shapes else ""
            findings.append(
                Finding(
                    check="format_change",
                    source_column=source,
                    target_column=target,
                    action="propose",
                    message=(
                        f"Values in '{source}' changed format: the saved transform for "
                        f"'{target}' now fails on {_pct(now.failure_rate)} of them (it was "
                        f"{_pct(before['failure_rate'])}).{shapes} The agent proposes a new "
                        "transform."
                    ),
                    details={
                        "baseline": before["failure_rate"],
                        "current": now.failure_rate,
                        "new_patterns": new_shapes,
                        "failing_values": now.failing_values,
                        "transform_sql": m["transform_sql"],
                    },
                )
            )

    for name, column in new_columns.items():
        if name not in old_columns:
            continue
        before, now_rate = old_columns[name]["empty_rate"], empty_rate(column)
        if now_rate - before > EMPTY_RATE_JUMP and _significant_rise(
            before, baseline["row_count"], now_rate, column.row_count
        ):
            findings.append(
                Finding(
                    check="null_rate_jump",
                    source_column=name,
                    target_column=None,
                    action="warn",
                    message=(
                        f"'{name}' is empty in {_pct(now_rate)} of rows (it was {_pct(before)})."
                        " The load continues."
                    ),
                    details={"baseline": before, "current": now_rate},
                )
            )

    if recent_row_counts:
        average = sum(recent_row_counts) / len(recent_row_counts)
        ratio = profile.row_count / average if average else 1.0
        if ratio < VOLUME_LOW or ratio > VOLUME_HIGH:
            findings.append(
                Finding(
                    check="volume_anomaly",
                    source_column=None,
                    target_column=None,
                    action="warn",
                    message=(
                        f"This file has {profile.row_count:,} rows; recent loads averaged "
                        f"{round(average):,} ({_pct(ratio)}). The load continues."
                    ),
                    details={
                        "rows": profile.row_count,
                        "recent_average": round(average, 1),
                        "ratio": round(ratio, 2),
                    },
                )
            )

    order = {"block": 0, "propose": 1, "warn": 2}
    return sorted(findings, key=lambda f: order[f.action])
