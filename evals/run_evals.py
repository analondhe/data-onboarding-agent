"""Run the agent over every eval case and write a scorecard.

Each case goes through the same runner as the CLI: profile, map, review (answered from the
answer key), and dbt build. The stored mappings and defects and the loaded table are then
scored against the answer key and the clean table. Results go to
``<results-dir>/<timestamp>_<model>.md`` and ``.json`` and to MLflow, tagged with the model.

  python -m evals.run_evals                  # real model from LLM_MODEL / LLM_BASE_URL
  python -m evals.run_evals --repeats 3      # adds consistency over 3 runs per case
  python -m evals.run_evals --fake --limit 5 --results-dir data/eval-smoke   # CI smoke test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path
from typing import Any

from agent.llm import LLM, FakeLLM, OpenAICompatibleLLM
from agent.runner import onboard
from agent.settings import Settings
from agent.tracing import Tracer
from api.db import session_factory
from evals.oracle import answer_key_reviewer, oracle_responder
from evals.scoring import CaseScore, aggregate, score_case, score_cells
from tools.warehouse import Warehouse

EXPERIMENT = "data-onboarding-evals"


# --- Scorecard -------------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def render_markdown(meta: dict[str, Any], summary: dict[str, Any], scores: list[CaseScore]) -> str:
    repeats = f", {meta['repeats']} runs each" if meta["repeats"] > 1 else ""
    lines = [
        f"# Scorecard: {meta['model']}",
        "",
        f"{meta['timestamp']} · {summary['cases']} cases{repeats} · review threshold "
        f"{meta['threshold']} · {summary['failed_runs']} failed runs · {summary['repairs']} "
        f"repairs · {summary['model_calls']} model calls · {summary['tokens']} tokens · "
        f"{summary['duration_seconds']}s",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Mapping accuracy | {_pct(summary['mapping_accuracy'])} |",
        f"| Mapping accuracy, names changed | {_pct(summary['mapping_accuracy_names_changed'])} |",
        "| Mapping accuracy, names unchanged | "
        f"{_pct(summary['mapping_accuracy_names_unchanged'])} |",
        f"| Defect recall | {_pct(summary['defect_recall'])} |",
        f"| Defect precision | {_pct(summary['defect_precision'])} |",
        f"| Cell accuracy | {_pct(summary['cell_accuracy'])} |",
        f"| Escalation rate | {_pct(summary['escalation_rate'])} |",
        f"| Bad auto-approvals | {summary['bad_auto_approvals']} |",
        f"| Build success | {_pct(summary['build_success'])} |",
        f"| Consistency | {_pct(summary['consistency'])} |",
        "",
        "## Runs",
        "",
        "| Case | Run | Corruptions | Status | Mapping | Defects found | Flagged correct "
        "| Escalated | Bad auto | Loaded / rejected | Cells | Repairs | Calls | Seconds |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in scores:
        cells = _pct(s.cells_correct / s.cells_total) if s.cells_total else "n/a"
        lines.append(
            f"| {s.case_id} | {s.repeat} | {', '.join(s.corruptions)} | {s.status} "
            f"| {s.mapping_correct}/{s.source_columns} | {s.defects_recalled}/{s.defects_injected} "
            f"| {s.defects_flagged_correct}/{s.defects_flagged} | {s.escalated}/{s.mappings_total} "
            f"| {s.bad_auto_approvals} | {s.rows_loaded} / {s.rows_rejected} | {cells} "
            f"| {s.repairs} | {s.model_calls} | {s.duration_seconds} |"
        )
    problems = [s for s in scores if s.error or s.wrong_mappings or s.missed_defects]
    problems += [s for s in scores if s.spurious_defects and s not in problems]
    if problems:
        lines += ["", "## Errors", ""]
    for s in problems:
        label = f"{s.case_id}" + (f" (run {s.repeat})" if meta["repeats"] > 1 else "")
        if s.error:
            lines.append(f"- **{label}** failed: {s.error}")
        for w in s.wrong_mappings:
            auto = (
                f" (auto-approved at {w['auto_approved_confidence']:.2f})"
                if "auto_approved_confidence" in w
                else ""
            )
            lines.append(
                f"- **{label}** `{w['source']}`: predicted {w['predicted'] or 'unmapped'}, "
                f"truth {w['truth'] or 'unmapped'}{auto}"
            )
        if s.missed_defects:
            lines.append(f"- **{label}** missed defects: {', '.join(s.missed_defects)}")
        if s.spurious_defects:
            lines.append(f"- **{label}** spurious defects: {', '.join(s.spurious_defects)}")
    return "\n".join(lines) + "\n"


# --- Harness ---------------------------------------------------------------------------------


def select_cases(cases_dir: Path, names: list[str] | None, limit: int | None) -> list[Path]:
    """Case directories interleaved across tables, so a --limit still covers every table."""
    dirs = sorted(p for p in cases_dir.iterdir() if (p / "answer_key.json").is_file())
    if names:
        dirs = [d for d in dirs if d.name in names]
    by_table: dict[str, list[Path]] = {}
    for d in dirs:
        by_table.setdefault(d.name.rsplit("_", 1)[0], []).append(d)
    interleaved = [d for group in zip_longest(*by_table.values()) for d in group if d]
    return interleaved[:limit] if limit else interleaved


def run_evals(
    case_dirs: list[Path],
    settings: Settings,
    make_llm: Callable[[dict[str, Any]], LLM],
    results_dir: Path,
    repeats: int = 1,
) -> tuple[dict[str, Any], list[CaseScore], Path]:
    """Run every case ``repeats`` times; return (summary, run scores, markdown scorecard)."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    eval_dir = settings.data_dir / "evals"
    eval_settings = replace(
        settings, data_dir=eval_dir, database_url=f"sqlite:///{eval_dir / 'app.db'}"
    )
    tracer = Tracer(settings.mlflow_tracking_uri, experiment=EXPERIMENT)
    sessions = session_factory(eval_settings.database_url)
    keys = [json.loads((d / "answer_key.json").read_text()) for d in case_dirs]
    model_name = make_llm(keys[0]).model
    meta = {
        "model": model_name,
        "timestamp": timestamp,
        "threshold": settings.review_confidence_threshold,
        "repeats": repeats,
        "cases": [k["case_id"] for k in keys],
    }

    scores: list[CaseScore] = []
    with tracer.experiment_run(
        f"eval-{model_name}-{timestamp}",
        tags={"model": model_name},
        params={"model": model_name, "threshold": meta["threshold"], "repeats": repeats},
    ):
        for case_dir, key in zip(case_dirs, keys, strict=True):
            for repeat in range(1, repeats + 1):
                run_id = f"eval-{timestamp}-{key['case_id']}" + (
                    f"-r{repeat}" if repeats > 1 else ""
                )
                with sessions() as session:
                    run = onboard(
                        session,
                        eval_settings,
                        make_llm(key),
                        tracer,
                        customer_name=f"eval:{key['case_id']}",
                        schema_name=key["table"],
                        paths=[case_dir / key["messy_file"]],
                        run_id=run_id,
                        review=answer_key_reviewer(key),
                    )
                    score = score_case(key, run, settings.review_confidence_threshold)
                score.repeat = repeat
                score_cells(score, key, case_dir, Warehouse.for_run(run_id, eval_dir))
                scores.append(score)
                cells = f"{score.cells_correct}/{score.cells_total}"
                print(
                    f"{score.case_id:<18} r{repeat} {score.status:<9} mapping "
                    f"{score.mapping_correct}/{score.source_columns}  defects "
                    f"{score.defects_recalled}/{score.defects_injected}  cells {cells}  "
                    f"repairs {score.repairs}  {score.duration_seconds}s",
                    flush=True,
                )
        summary = aggregate(scores)
        tracer.log_metrics({k: v for k, v in summary.items() if isinstance(v, int | float)})

    results_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{timestamp}_{re.sub(r'[^A-Za-z0-9.-]+', '-', model_name)}"
    report = {"meta": meta, "summary": summary, "runs": [asdict(s) for s in scores]}
    (results_dir / f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n")
    markdown = results_dir / f"{stem}.md"
    markdown.write_text(render_markdown(meta, summary, scores))
    return summary, scores, markdown


def smoke_failures(summary: dict[str, Any]) -> list[str]:
    """What an answer-key run got wrong; it must map, flag, and build everything."""
    checks = {
        "failed_runs": summary["failed_runs"] == 0,
        "mapping_accuracy": summary["mapping_accuracy"] == 1.0,
        "defect_recall": summary["defect_recall"] in (1.0, None),
        "defect_precision": summary["defect_precision"] in (1.0, None),
        "bad_auto_approvals": summary["bad_auto_approvals"] == 0,
        "build_success": summary["build_success"] == 1.0,
    }
    return [name for name, ok in checks.items() if not ok]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score the agent on the eval cases.")
    parser.add_argument("--cases-dir", type=Path, default=Path("evals/cases"))
    parser.add_argument("--results-dir", type=Path, default=Path("evals/results"))
    parser.add_argument("--case", action="append", dest="cases", help="run only these cases")
    parser.add_argument("--limit", type=int, help="run at most this many cases")
    parser.add_argument("--repeats", type=int, default=1, help="runs per case, for consistency")
    parser.add_argument(
        "--fake",
        action="store_true",
        help="answer with FakeLLM from the answer keys; exit 1 unless mapping, defects, "
        "and builds are all perfect",
    )
    args = parser.parse_args(argv)

    if not args.cases_dir.is_dir():
        print(f"No cases in {args.cases_dir}; run `make seed` first", file=sys.stderr)
        return 2
    case_dirs = select_cases(args.cases_dir, args.cases, args.limit)
    if not case_dirs:
        print("No matching cases", file=sys.stderr)
        return 2
    settings = Settings.from_env()

    if args.fake:

        def make_llm(key: dict[str, Any]) -> LLM:
            return FakeLLM(oracle_responder(key), model="fake-oracle")

    else:
        try:
            real = OpenAICompatibleLLM.from_settings(settings)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2

        def make_llm(key: dict[str, Any]) -> LLM:
            return real

    summary, _, markdown = run_evals(
        case_dirs, settings, make_llm, args.results_dir, repeats=args.repeats
    )
    print(f"\nScorecard: {markdown}")
    print(json.dumps(summary, indent=1))
    if args.fake and (failed := smoke_failures(summary)):
        print(f"Smoke eval failed on: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
