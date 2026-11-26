"""Run the agent over every eval case and write a scorecard.

Each case goes through the same runner as the CLI (profile, map, persist), then the stored
mappings and defects are scored against the case's answer key. Results go to
``<results-dir>/<timestamp>_<model>.md`` and ``.json`` and to MLflow, tagged with the model.

  python -m evals.run_evals                 # real model from LLM_MODEL / LLM_BASE_URL
  python -m evals.run_evals --fake --limit 5 --results-dir data/eval-smoke   # CI smoke test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path
from typing import Any

from agent.defects import DefectType
from agent.llm import LLM, FakeLLM, FakeReply, FakeStep, Message, OpenAICompatibleLLM
from agent.runner import onboard
from agent.settings import Settings
from agent.tracing import Tracer
from api.db import session_factory
from api.models import Run
from datagen.schemas import load_schema

EXPERIMENT = "data-onboarding-evals"


# --- Scoring -------------------------------------------------------------------------------


@dataclass
class CaseScore:
    case_id: str
    table: str
    names_changed: bool
    corruptions: list[str]
    status: str
    error: str | None
    source_columns: int = 0
    mapping_correct: int = 0
    mappings_total: int = 0  # proposed mappings plus missing-target rows
    escalated: int = 0
    bad_auto_approvals: int = 0
    defects_injected: int = 0
    defects_recalled: int = 0
    defects_flagged: int = 0
    defects_flagged_correct: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    tokens: int = 0
    duration_seconds: float = 0.0
    wrong_mappings: list[dict[str, Any]] = field(default_factory=list)
    missed_defects: list[str] = field(default_factory=list)
    spurious_defects: list[str] = field(default_factory=list)


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
        model_calls=run.model_calls,
        tool_calls=run.tool_calls,
        tokens=run.prompt_tokens + run.completion_tokens,
        duration_seconds=run.duration_seconds or 0.0,
    )
    auto_approved = {
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
        if source in auto_approved:
            wrong["auto_approved_confidence"] = auto_approved[source]
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
            where = f.column_name or f.target_column or "-"
            score.spurious_defects.append(f"{f.defect_type}@{where}")
    return score


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def aggregate(scores: list[CaseScore]) -> dict[str, Any]:
    """Micro-averaged metrics: totals over all cases, not an average of per-case ratios."""

    def total(attr: str, subset: list[CaseScore] | None = None) -> float:
        return sum(getattr(s, attr) for s in (scores if subset is None else subset))

    renamed = [s for s in scores if s.names_changed]
    kept = [s for s in scores if not s.names_changed]
    return {
        "cases": len(scores),
        "failed_runs": sum(1 for s in scores if s.status == "failed"),
        "mapping_accuracy": _ratio(total("mapping_correct"), total("source_columns")),
        "mapping_accuracy_names_changed": _ratio(
            total("mapping_correct", renamed), total("source_columns", renamed)
        ),
        "mapping_accuracy_names_unchanged": _ratio(
            total("mapping_correct", kept), total("source_columns", kept)
        ),
        "defect_recall": _ratio(total("defects_recalled"), total("defects_injected")),
        "defect_precision": _ratio(total("defects_flagged_correct"), total("defects_flagged")),
        "escalation_rate": _ratio(total("escalated"), total("mappings_total")),
        "bad_auto_approvals": int(total("bad_auto_approvals")),
        "model_calls": int(total("model_calls")),
        "tokens": int(total("tokens")),
        "duration_seconds": round(total("duration_seconds"), 1),
    }


# --- Scorecard -----------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def render_markdown(meta: dict[str, Any], summary: dict[str, Any], scores: list[CaseScore]) -> str:
    lines = [
        f"# Scorecard: {meta['model']}",
        "",
        f"{meta['timestamp']} · {summary['cases']} cases · review threshold {meta['threshold']}"
        f" · {summary['failed_runs']} failed runs · {summary['model_calls']} model calls"
        f" · {summary['tokens']} tokens · {summary['duration_seconds']}s",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Mapping accuracy | {_pct(summary['mapping_accuracy'])} |",
        f"| Mapping accuracy, names changed | {_pct(summary['mapping_accuracy_names_changed'])} |",
        "| Mapping accuracy, names unchanged | "
        f"{_pct(summary['mapping_accuracy_names_unchanged'])} |",
        f"| Defect recall | {_pct(summary['defect_recall'])} |",
        f"| Defect precision | {_pct(summary['defect_precision'])} |",
        f"| Escalation rate | {_pct(summary['escalation_rate'])} |",
        f"| Bad auto-approvals | {summary['bad_auto_approvals']} |",
        "",
        "## Cases",
        "",
        "| Case | Corruptions | Status | Mapping | Defects found | Flagged correct "
        "| Escalated | Bad auto | Calls | Seconds |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in scores:
        lines.append(
            f"| {s.case_id} | {', '.join(s.corruptions)} | {s.status} "
            f"| {s.mapping_correct}/{s.source_columns} | {s.defects_recalled}/{s.defects_injected} "
            f"| {s.defects_flagged_correct}/{s.defects_flagged} | {s.escalated}/{s.mappings_total} "
            f"| {s.bad_auto_approvals} | {s.model_calls} | {s.duration_seconds} |"
        )
    problems = [s for s in scores if s.error or s.wrong_mappings or s.missed_defects]
    problems += [s for s in scores if s.spurious_defects and s not in problems]
    if problems:
        lines += ["", "## Errors", ""]
    for s in problems:
        if s.error:
            lines.append(f"- **{s.case_id}** failed: {s.error}")
        for w in s.wrong_mappings:
            auto = (
                f" (auto-approved at {w['auto_approved_confidence']:.2f})"
                if "auto_approved_confidence" in w
                else ""
            )
            lines.append(
                f"- **{s.case_id}** `{w['source']}`: predicted {w['predicted'] or 'unmapped'}, "
                f"truth {w['truth'] or 'unmapped'}{auto}"
            )
        if s.missed_defects:
            lines.append(f"- **{s.case_id}** missed defects: {', '.join(s.missed_defects)}")
        if s.spurious_defects:
            lines.append(f"- **{s.case_id}** spurious defects: {', '.join(s.spurious_defects)}")
    return "\n".join(lines) + "\n"


# --- Fake model that answers from the answer key -------------------------------------------


def oracle_responder(key: dict[str, Any]) -> Callable[[list[Message]], FakeStep]:
    """FakeLLM script: call two tools, then submit exactly what the answer key says."""
    schema = {c["name"]: c for c in load_schema(key["table"])["columns"]}

    def sql(m: dict[str, Any]) -> str:
        column = '"' + m["source_column"].replace('"', '""') + '"'
        if "merged" in m:
            sep = m["merged"]["separator"].replace("'", "''")
            return f"trim(split_part({column}, '{sep}', {m['merged']['part']}))"
        return f"trim({column})"

    def respond(messages: list[Message]) -> FakeStep:
        match = re.search(r"file_id '([0-9a-f]+)'", messages[1]["content"])
        file_id = match.group(1) if match else ""
        first = key["mappings"][0]
        if not any(m["role"] == "assistant" for m in messages):
            return FakeReply(
                tool_calls=[
                    ("get_column_profile", {"file_id": file_id, "column": first["source_column"]}),
                    (
                        "test_transform",
                        {
                            "file_id": file_id,
                            "sql_expression": sql(first),
                            "target_type": schema[first["target_column"]]["type"],
                        },
                    ),
                ]
            )
        submission = {
            "mappings": [
                {
                    "source_column": m["source_column"],
                    "target_column": m["target_column"],
                    "transform_sql": sql(m),
                    "confidence": 0.95,
                    "reason": "answer key",
                }
                for m in key["mappings"]
            ],
            "missing_required": [
                {"target_column": t, "reason": "no source column"}
                for t in key["missing_target_columns"]
            ],
            "defects": [
                {
                    "defect_type": d["type"],
                    "source_column": d["source_column"],
                    "target_column": d["target_columns"][0],
                    "affected_rows": d["affected_rows"],
                    "description": "answer key",
                }
                for d in key["defects"]
            ],
        }
        return FakeReply(tool_calls=[("submit_mappings", submission)])

    return respond


# --- Harness -------------------------------------------------------------------------------


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
) -> tuple[dict[str, Any], list[CaseScore], Path]:
    """Run every case; return (summary, per-case scores, markdown scorecard path)."""
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
        "cases": [k["case_id"] for k in keys],
    }

    scores: list[CaseScore] = []
    with tracer.experiment_run(
        f"eval-{model_name}-{timestamp}",
        tags={"model": model_name},
        params={"model": model_name, "threshold": meta["threshold"]},
    ):
        for case_dir, key in zip(case_dirs, keys, strict=True):
            with sessions() as session:
                run = onboard(
                    session,
                    eval_settings,
                    make_llm(key),
                    tracer,
                    customer_name=f"eval:{key['case_id']}",
                    schema_name=key["table"],
                    paths=[case_dir / key["messy_file"]],
                    run_id=f"eval-{timestamp}-{key['case_id']}",
                )
                score = score_case(key, run, settings.review_confidence_threshold)
            scores.append(score)
            print(
                f"{score.case_id:<18} {score.status:<16} mapping "
                f"{score.mapping_correct}/{score.source_columns}  defects "
                f"{score.defects_recalled}/{score.defects_injected}  {score.duration_seconds}s",
                flush=True,
            )
        summary = aggregate(scores)
        tracer.log_metrics({k: v for k, v in summary.items() if isinstance(v, int | float)})

    results_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{timestamp}_{re.sub(r'[^A-Za-z0-9.-]+', '-', model_name)}"
    report = {"meta": meta, "summary": summary, "cases": [asdict(s) for s in scores]}
    (results_dir / f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n")
    markdown = results_dir / f"{stem}.md"
    markdown.write_text(render_markdown(meta, summary, scores))
    return summary, scores, markdown


def is_perfect(summary: dict[str, Any]) -> bool:
    return (
        summary["failed_runs"] == 0
        and summary["mapping_accuracy"] == 1.0
        and summary["defect_recall"] in (1.0, None)
        and summary["defect_precision"] in (1.0, None)
        and summary["bad_auto_approvals"] == 0
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score the agent on the eval cases.")
    parser.add_argument("--cases-dir", type=Path, default=Path("evals/cases"))
    parser.add_argument("--results-dir", type=Path, default=Path("evals/results"))
    parser.add_argument("--case", action="append", dest="cases", help="run only these cases")
    parser.add_argument("--limit", type=int, help="run at most this many cases")
    parser.add_argument(
        "--fake",
        action="store_true",
        help="answer with FakeLLM from the answer keys; exit 1 unless every score is perfect",
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

    summary, _, markdown = run_evals(case_dirs, settings, make_llm, args.results_dir)
    print(f"\nScorecard: {markdown}")
    print(json.dumps(summary, indent=1))
    if args.fake and not is_perfect(summary):
        print("Smoke eval failed: the answer-key run should score perfectly", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
