"""Command line: onboard files, print the proposed mappings and defects, and optionally build.

Usage: python -m agent.cli onboard --file path.csv --schema customers [--approve-all]

Without --approve-all the run stops at awaiting_review. With it, every proposed mapping is
approved (recorded in review_events), dbt builds the clean table, and the report shows rows
loaded and rejected, dbt test results, and repairs.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from agent.llm import OpenAICompatibleLLM
from agent.review import approve_all
from agent.runner import onboard
from agent.settings import Settings
from agent.tracing import Tracer
from api.db import session_factory
from api.models import Mapping, Run, RunStatus


def _clip(text: str | None, width: int) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def format_report(run: Run) -> str:
    lines = [
        f"Run {run.id}: {run.status}" + (f" ({run.error})" if run.error else ""),
        f"Model {run.model_name}: {run.model_calls} calls, {run.tool_calls} tool calls, "
        f"{run.prompt_tokens} prompt + {run.completion_tokens} completion tokens, "
        f"{run.duration_seconds}s. Trace {run.trace_id or 'disabled'}",
    ]
    mappings: list[Mapping] = sorted(
        run.mappings, key=lambda m: (m.status != "proposed", m.confidence or 0)
    )
    if mappings:
        lines += ["", "Mappings (review first):"]
        lines.append(f"  {'target':<20} {'source':<26} {'conf':>5}  {'status':<9} transform_sql")
        for m in mappings:
            conf = f"{m.confidence:.2f}" if m.confidence is not None else "  -"
            lines.append(
                f"  {m.target_column:<20} {_clip(m.source_column or '(none)', 26):<26} "
                f"{conf:>5}  {m.status:<9} "
                f"{_clip(m.transform_sql, 70)}"
            )
            lines.append(f"  {'':<20} {_clip(m.reason, 110)}")
    if run.defects:
        lines += ["", "Defects:"]
        for d in run.defects:
            where = d.column_name or d.target_column or "-"
            rows = f", ~{d.affected_rows} rows" if d.affected_rows is not None else ""
            lines.append(f"  {d.defect_type:<24} {where}{rows}: {_clip(d.description, 90)}")
    if run.build_report:
        lines += ["", *format_build(run.build_report)]
    return "\n".join(lines)


def format_build(report: dict) -> list[str]:
    dbt = report["dbt"]
    lines = [
        f"Build: {report['status']} after {report['repairs']} repairs. dbt models "
        f"{dbt['models_passed']} passed / {dbt['models_failed']} failed, tests "
        f"{dbt['tests_passed']} passed / {dbt['tests_failed']} failed"
    ]
    for r in dbt["results"]:
        if r["status"] not in ("success", "pass"):
            lines.append(f"  {r['kind']} {r['name']}: {r['status']} {_clip(r['message'], 80)}")
    for a in report["row_accounting"]:
        lines.append(
            f"Rows for file {a['file_id']}: {a['rows_in']} in = {a['loaded']} loaded + "
            f"{a['rejected']} rejected ({'balanced' if a['balanced'] else 'NOT BALANCED'})"
        )
        for reason, n in a["reject_reasons"].items():
            lines.append(f"  {n:>6}  {_clip(reason, 100)}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Onboard customer data files.")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("onboard", help="profile and map one or more files")
    cmd.add_argument("--file", type=Path, action="append", required=True, dest="files")
    cmd.add_argument("--schema", required=True, help="target schema, e.g. customers")
    cmd.add_argument("--customer", default="Acme Cloud")
    cmd.add_argument(
        "--approve-all",
        action="store_true",
        help="approve every proposed mapping and build the clean table with dbt",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    for noisy in ("httpx", "mlflow", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        llm = OpenAICompatibleLLM.from_settings(settings)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    sessions = session_factory(settings.database_url)
    with sessions() as session:
        run = onboard(
            session,
            settings,
            llm,
            Tracer(settings.mlflow_tracking_uri),
            customer_name=args.customer,
            schema_name=args.schema,
            paths=args.files,
            review=approve_all if args.approve_all else None,
        )
        print(format_report(run))
    expected = RunStatus.DONE if args.approve_all else RunStatus.AWAITING_REVIEW
    return 0 if run.status == expected else 1


if __name__ == "__main__":
    sys.exit(main())
