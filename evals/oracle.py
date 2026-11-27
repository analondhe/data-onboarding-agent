"""Answers from the answer key: the eval's human reviewer, and a FakeLLM that maps perfectly.

The reviewer stands in for the human review step: it approves proposed mappings the answer key
confirms and rejects the rest. The oracle responder lets ``make eval-smoke`` exercise the whole
pipeline (load, profile, loop, validation, review, dbt build, scoring) without a model.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from agent.llm import FakeReply, FakeStep, Message
from agent.review import Decision, apply_review
from api.models import MappingStatus, Run
from datagen.schemas import load_schema

NULL_TOKENS_SQL = "('', 'null', 'none', 'n/a', 'na', '-', 'nan')"
DATE_FORMATS_SQL = "['%Y-%m-%d', '%m/%d/%Y', '%d %b %Y', '%Y%m%d', '%B %d, %Y']"


def answer_key_reviewer(key: dict[str, Any]) -> Callable[[Session, Run], None]:
    """Approve proposed mappings the answer key confirms; reject the others."""
    true_pairs = {(m["source_column"], m["target_column"]) for m in key["mappings"]}
    missing = set(key["missing_target_columns"])

    def review(session: Session, run: Run) -> None:
        decisions = []
        for m in run.mappings:
            if m.status != MappingStatus.PROPOSED:
                continue
            if m.source_column is None:
                correct = m.target_column in missing
            else:
                correct = (m.source_column, m.target_column) in true_pairs
            decisions.append(Decision(mapping_id=m.id, action="approve" if correct else "reject"))
        apply_review(session, run, decisions, actor="answer-key")

    return review


def oracle_sql(mapping: dict[str, Any], spec: dict[str, Any], variants: dict[str, str]) -> str:
    """A transform that cleans every corruption the generator injects into this column."""
    column = '"' + mapping["source_column"].replace('"', '""') + '"'
    if "merged" in mapping:
        sep = mapping["merged"]["separator"].replace("'", "''")
        column = f"split_part({column}, '{sep}', {mapping['merged']['part']})"
    v = f"trim({column})"
    kind = spec["type"]
    if kind == "date":
        value = f"try_strptime({v}, {DATE_FORMATS_SQL})::DATE"
    elif kind.startswith("decimal"):
        value = f"try_cast(regexp_replace({v}, '[^0-9.-]', '', 'g') AS DECIMAL(10, 2))"
    elif kind == "integer":
        value = f"try_cast(try_cast(regexp_replace({v}, '[^0-9.-]', '', 'g') AS DOUBLE) AS INTEGER)"
    elif kind == "boolean":
        value = (
            f"CASE WHEN lower({v}) IN ('true', 't', 'yes', 'y', '1') THEN true "
            f"WHEN lower({v}) IN ('false', 'f', 'no', 'n', '0') THEN false END"
        )
    elif "allowed_values" in spec:
        whens = " ".join(
            f"WHEN '{variant.strip().lower().replace(chr(39), chr(39) * 2)}' THEN '{canonical}'"
            for variant, canonical in sorted(variants.items())
        )
        value = f"CASE lower({v}) {whens} ELSE lower({v}) END" if whens else f"lower({v})"
    elif spec["name"].endswith("_id"):
        value = f"upper({v})"
    else:
        value = v
    return f"CASE WHEN lower({v}) IN {NULL_TOKENS_SQL} THEN NULL ELSE {value} END"


def oracle_responder(key: dict[str, Any]) -> Callable[[list[Message]], FakeStep]:
    """FakeLLM script: call two tools, then submit exactly what the answer key says.

    If a build ever needs repair, it gives up with submit_build so the failure shows up.
    """
    schema = {c["name"]: c for c in load_schema(key["table"])["columns"]}
    variants: dict[str, dict[str, str]] = {}
    for d in key["defects"]:
        if d["type"] == "value_drift":
            variants.setdefault(d["target_columns"][0], {}).update(d["details"]["variants"])

    def sql(m: dict[str, Any]) -> str:
        target = m["target_column"]
        return oracle_sql(m, schema[target], variants.get(target, {}))

    def respond(messages: list[Message]) -> FakeStep:
        if messages[0]["content"].startswith("You repair a failing dbt build"):
            return FakeReply(tool_calls=[("submit_build", {"summary": "oracle does not repair"})])
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
