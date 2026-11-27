"""Drift eval: second batches of a saved source, each with one known change or none.

``python -m evals.drift build`` writes the cases (``make seed`` runs it):

    evals/drift_cases/<table>/base.csv                the batch each source is saved from
    evals/drift_cases/<table>/<case>/batch.csv        a later batch
    evals/drift_cases/<table>/<case>/expected.json    the findings it must raise, and fixes

Every table gets three unchanged batches (fresh samples of the same data; any finding there is a
false alarm) and one batch per drift type. Batches use the target column names and clean
values: the base mapping is not what is tested here.

``python -m evals.drift run`` (``make eval-drift``) onboards each base batch with the answer key,
saves it as one source per case, loads every case's batch as a recurring load, approves the
proposals the answer key confirms, and writes a scorecard to evals/results/.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from agent import contracts
from agent.llm import LLM, FakeLLM, FakeReply, FakeStep, Message, OpenAICompatibleLLM
from agent.review import Decision, apply_review, finish_review
from agent.runner import advance, create_load, onboard
from agent.settings import Settings
from agent.tracing import Tracer
from api.db import session_factory
from api.models import MappingStatus, Run, RunStatus
from datagen.schemas import TABLES, key_column, load_schema
from evals.corrupt import read_clean_csv, sample_clean
from evals.oracle import DATE_FORMATS_SQL, answer_key_reviewer, oracle_responder, oracle_sql

UNCHANGED_BATCHES = 3
NEW_DATE_FORMAT = "%d.%m.%Y"  # a format no saved transform handles

# --- Cases -----------------------------------------------------------------------------------


def _columns(schema: dict[str, Any]) -> dict[str, str]:
    """The columns each drift type changes, picked from the schema so every table has them."""
    specs = schema["columns"]
    key = key_column(schema)
    required = [c["name"] for c in specs if c.get("required") and c["name"] != key]
    return {
        "required": required[0],
        "optional": next(c["name"] for c in specs if not c.get("required")),
        "date": next(c["name"] for c in specs if c["type"] == "date"),
        "category": next(c["name"] for c in specs if c.get("allowed_values")),
        "blank": required[-1],
    }


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _sql(column: str, spec: dict[str, Any], variants: dict[str, str] | None = None) -> str:
    return oracle_sql(
        {"source_column": column, "target_column": spec["name"]}, spec, variants or {}
    )


def table_cases(
    schema: dict[str, Any], clean: list[dict[str, str]], rows: int, seed: int
) -> tuple[list[dict[str, str]], dict[str, tuple[list[dict[str, str]], dict[str, Any]]]]:
    """The base batch and every case of one table: {case: (rows, expected)}."""
    specs = {c["name"]: c for c in schema["columns"]}
    pool = sample_clean(clean, rows * (UNCHANGED_BATCHES + 2), seed)
    base, rest = pool[:rows], pool[rows:]

    def batch(i: int) -> list[dict[str, str]]:
        return [dict(r) for r in sample_clean(rest, rows, seed + i + 1)]

    cols = _columns(schema)
    cases: dict[str, tuple[list[dict[str, str]], dict[str, Any]]] = {}

    def case(
        name: str,
        data: list[dict[str, str]],
        findings: list[list[Any]],
        proposals: list[dict[str, str]] | None = None,
        blocked: bool = False,
    ) -> None:
        cases[name] = (
            data,
            {"findings": findings, "proposals": proposals or [], "blocked": blocked},
        )

    for i in range(UNCHANGED_BATCHES):
        case(f"unchanged_{i + 1}", batch(i), [])

    data = batch(10)
    for n, r in enumerate(data):
        r["legacy_notes"] = f"imported from the old CRM, batch {n % 7}"
    case("column_added", data, [["column_added", "legacy_notes"]])

    gone = cols["required"]
    data = [{k: v for k, v in r.items() if k != gone} for r in batch(11)]
    case("column_missing_required", data, [["column_missing", gone]], blocked=True)

    gone = cols["optional"]
    data = [{k: v for k, v in r.items() if k != gone} for r in batch(12)]
    case("column_missing_optional", data, [["column_missing", gone]])

    old = cols["date"]
    new = old.replace("_", " ").title()
    data = [{(new if k == old else k): v for k, v in r.items()} for r in batch(13)]
    rename_sql = _sql(new, specs[old])
    case(
        "column_renamed",
        data,
        [["column_renamed", new]],
        [{"source_column": new, "target_column": old, "transform_sql": rename_sql}],
    )

    column = cols["date"]
    data = batch(14)
    for r in data:
        if r[column]:
            r[column] = date.fromisoformat(r[column]).strftime(NEW_DATE_FORMAT)
    formats = DATE_FORMATS_SQL.replace("]", f", '{NEW_DATE_FORMAT}']")
    fixed = f'try_strptime(trim("{column}"), {formats})::DATE'
    case(
        "format_change",
        data,
        [["format_change", column]],
        [{"source_column": column, "target_column": column, "transform_sql": fixed}],
    )

    column = cols["category"]
    first = specs[column]["allowed_values"][0]
    variant = f"{first.title()} (legacy)"
    data = batch(15)
    changed = 0
    for r in data:
        if r[column] == first and changed % 3 == 0:
            r[column] = variant
        changed += r[column] in (first, variant)
    case(
        "new_category",
        data,
        [["new_category", column]],
        [{
            "source_column": column, "target_column": column,
            "transform_sql": _sql(column, specs[column], {variant: first}),
        }],
    )  # fmt: skip

    column = cols["blank"]
    data = batch(16)
    for n, r in enumerate(data):
        if n % 10 < 3:
            r[column] = ""
    case("null_rate_jump", data, [["null_rate_jump", column]])

    case("volume_anomaly", batch(17)[: rows // 4], [["volume_anomaly", None]])
    return base, cases


def build_cases(clean_dir: Path, out: Path, rows: int, seed: int) -> int:
    count = 0
    for table in TABLES:
        schema = load_schema(table)
        base, cases = table_cases(schema, read_clean_csv(clean_dir / f"{table}.csv"), rows, seed)
        _write(out / table / "base.csv", base)
        for name, (data, expected) in cases.items():
            _write(out / table / name / "batch.csv", data)
            (out / table / name / "expected.json").write_text(
                json.dumps(expected, indent=1), encoding="utf-8"
            )
            count += 1
    return count


# --- Harness ---------------------------------------------------------------------------------


def _identity_key(table: str, columns: list[str]) -> dict[str, Any]:
    return {
        "table": table,
        "mappings": [{"source_column": c, "target_column": c} for c in columns],
        "missing_target_columns": [],
        "defects": [],
    }


def proposal_responder(expected: dict[str, Any]) -> Callable[[list[Message]], FakeStep]:
    """FakeLLM script for --fake: propose exactly the case's expected fixes."""

    def respond(messages: list[Message]) -> FakeStep:
        submission = {
            "mappings": [
                {**p, "confidence": 0.95, "reason": "answer key"} for p in expected["proposals"]
            ]
        }
        return FakeReply(tool_calls=[("submit_mappings", submission)])

    return respond


def review_proposals(session: Session, run: Run, expected: dict[str, Any]) -> tuple[int, int]:
    """Approve the proposals the case expects, reject the rest; returns (approved, proposed)."""
    wanted = {(p["source_column"], p["target_column"]) for p in expected["proposals"]}
    decisions = [
        Decision(
            mapping_id=m.id,
            action="approve" if (m.source_column, m.target_column) in wanted else "reject",
        )
        for m in run.mappings
        if m.status == MappingStatus.PROPOSED
    ]
    apply_review(session, run, decisions, actor="answer-key")
    finish_review(session, run)
    return sum(d.action == "approve" for d in decisions), len(decisions)


def run_case(
    session: Session,
    settings: Settings,
    llm: LLM,
    contract: Any,
    case_dir: Path,
    expected: dict[str, Any],
) -> dict[str, Any]:
    run = create_load(session, settings, contract, case_dir / "batch.csv")
    run = advance(session, settings, llm, Tracer(None), run)
    findings = [[f["check"], f["source_column"]] for f in run.drift or []]
    approved = proposed = 0
    if run.status == RunStatus.AWAITING_REVIEW:
        approved, proposed = review_proposals(session, run, expected)
        run = advance(session, settings, llm, Tracer(None), run)
    return {
        "case": f"{case_dir.parent.name}/{case_dir.name}",
        "kind": "unchanged" if case_dir.name.startswith("unchanged") else case_dir.name,
        "expected": expected["findings"],
        "found": findings,
        "blocked_expected": expected["blocked"],
        "status": run.status,
        "error": run.error,
        "model_calls": run.model_calls,
        "proposals": proposed,
        "proposals_approved": approved,
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _blocked(r: dict[str, Any]) -> bool:
    return r["status"] == RunStatus.FAILED and (r["error"] or "").startswith("Load blocked")


def score(results: list[dict[str, Any]]) -> dict[str, Any]:
    tp: Counter[str] = Counter()
    fp: Counter[str] = Counter()
    fn: Counter[str] = Counter()
    for r in results:
        expected = {tuple(f) for f in r["expected"]}
        found = {tuple(f) for f in r["found"]}
        for check, _ in expected & found:
            tp[check] += 1
        for check, _ in found - expected:
            fp[check] += 1
        for check, _ in expected - found:
            fn[check] += 1
    unchanged = [r for r in results if r["kind"] == "unchanged"]
    correct = [
        r
        for r in results
        if _blocked(r) == r["blocked_expected"]
        and (r["blocked_expected"] or r["status"] == RunStatus.DONE)
    ]
    checks = sorted(set(tp) | set(fp) | set(fn))
    total_tp, total_fp, total_fn = sum(tp.values()), sum(fp.values()), sum(fn.values())
    return {
        "cases": len(results),
        "detection_recall": _ratio(total_tp, total_tp + total_fn),
        "detection_precision": _ratio(total_tp, total_tp + total_fp),
        "false_alarm_rate": _ratio(sum(bool(r["found"]) for r in unchanged), len(unchanged)),
        "unchanged_loaded_without_model": sum(
            r["status"] == RunStatus.DONE and r["model_calls"] == 0 for r in unchanged
        ),
        "unchanged_cases": len(unchanged),
        "correct_outcomes": _ratio(len(correct), len(results)),
        "model_calls": sum(r["model_calls"] for r in results),
        "proposals_accepted": _ratio(
            sum(r["proposals_approved"] for r in results), sum(r["proposals"] for r in results)
        ),
        "per_check": {c: {"tp": tp[c], "fp": fp[c], "fn": fn[c]} for c in checks},
    }


def render_markdown(
    meta: dict[str, Any], summary: dict[str, Any], results: list[dict[str, Any]]
) -> str:
    def pct(v: float | None) -> str:
        return "-" if v is None else f"{v * 100:.1f}%"

    lines = [
        f"# Drift scorecard: {meta['model']}",
        "",
        f"{meta['timestamp']} · {summary['cases']} cases",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Detection recall | {pct(summary['detection_recall'])} |",
        f"| Detection precision | {pct(summary['detection_precision'])} |",
        f"| False alarms on unchanged batches | {pct(summary['false_alarm_rate'])} |",
        f"| Unchanged batches loaded with zero model calls | "
        f"{summary['unchanged_loaded_without_model']} of {summary['unchanged_cases']} |",
        f"| Correct outcome (loaded, or blocked when it should be) | "
        f"{pct(summary['correct_outcomes'])} |",
        f"| Proposals the answer key accepted | {pct(summary['proposals_accepted'])} |",
        f"| Model calls | {summary['model_calls']} |",
        "",
        "| Check | Found | False alarms | Missed |",
        "| --- | --- | --- | --- |",
        *(f"| {c} | {v['tp']} | {v['fp']} | {v['fn']} |" for c, v in summary["per_check"].items()),
        "",
        "## Cases",
        "",
        "| Case | Expected | Found | Status | Model calls |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in results:

        def fmt(items: list[list[Any]]) -> str:
            return ", ".join(f"{c}({s})" if s else c for c, s in items) or "none"

        lines.append(
            f"| {r['case']} | {fmt(r['expected'])} | {fmt(r['found'])} | {r['status']} | "
            f"{r['model_calls']} |"
        )
    return "\n".join(lines) + "\n"


def run_drift(
    cases_dir: Path, settings: Settings, llm: LLM | None, results_dir: Path
) -> tuple[dict[str, Any], Path]:
    """Run every case; ``llm=None`` answers proposals from the answer key (FakeLLM)."""

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    eval_dir = settings.data_dir / "evals-drift" / timestamp
    eval_settings = replace(
        settings, data_dir=eval_dir, database_url=f"sqlite:///{eval_dir / 'app.db'}"
    )
    session = session_factory(eval_settings.database_url)()
    results = []
    for table_dir in sorted(p for p in cases_dir.iterdir() if (p / "base.csv").is_file()):
        table = table_dir.name
        base_path = table_dir / "base.csv"
        columns = base_path.read_text(encoding="utf-8").splitlines()[0].split(",")
        key = _identity_key(table, columns)
        base = onboard(
            session, eval_settings, FakeLLM(oracle_responder(key)), Tracer(None),
            customer_name="Drift eval", schema_name=table, paths=[base_path],
            review=answer_key_reviewer(key),
        )  # fmt: skip
        if base.status != RunStatus.DONE:
            raise RuntimeError(f"Onboarding the {table} base batch failed: {base.error}")
        for case_dir in sorted(p for p in table_dir.iterdir() if p.is_dir()):
            expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
            contract = contracts.new_version(
                session, eval_settings, base, f"{table}/{case_dir.name}"
            )
            case_llm = llm or FakeLLM(proposal_responder(expected), model="fake-oracle")
            result = run_case(session, eval_settings, case_llm, contract, case_dir, expected)
            results.append(result)
            print(
                f"{result['case']}: {result['status']}, found "
                f"{result['found'] or 'nothing'}, {result['model_calls']} model calls",
                flush=True,
            )
    summary = score(results)
    meta = {
        "timestamp": timestamp,
        "model": llm.model if llm else "fake-oracle",
        "proposals": "fake answer key" if llm is None else "model",
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{timestamp}_drift_{meta['model'].replace('/', '-').replace(':', '-')}"
    (results_dir / f"{stem}.json").write_text(
        json.dumps({"meta": meta, "summary": summary, "cases": results}, indent=1, default=str),
        encoding="utf-8",
    )
    markdown = results_dir / f"{stem}.md"
    markdown.write_text(render_markdown(meta, summary, results), encoding="utf-8")
    return summary, markdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drift eval cases and harness.")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="write the drift cases from the clean tables")
    build.add_argument("--clean-dir", type=Path, default=Path("data/clean"))
    build.add_argument("--out", type=Path, default=Path("evals/drift_cases"))
    build.add_argument("--rows", type=int, default=1000)
    build.add_argument("--seed", type=int, default=42)
    run = sub.add_parser("run", help="load every case and write a scorecard")
    run.add_argument("--cases-dir", type=Path, default=Path("evals/drift_cases"))
    run.add_argument("--results-dir", type=Path, default=Path("evals/results"))
    run.add_argument("--fake", action="store_true", help="answer proposals from the answer key")
    args = parser.parse_args(argv)

    if args.command == "build":
        count = build_cases(args.clean_dir, args.out, args.rows, args.seed)
        print(f"Wrote {count} drift cases to {args.out}")
        return 0
    if not args.cases_dir.is_dir():
        print(f"No drift cases in {args.cases_dir}; run `make seed` first", file=sys.stderr)
        return 2
    settings = Settings.from_env()
    llm = None
    if not args.fake:
        try:
            llm = OpenAICompatibleLLM.from_settings(settings)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
    summary, markdown = run_drift(args.cases_dir, settings, llm, args.results_dir)
    print(f"\nScorecard: {markdown}")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
