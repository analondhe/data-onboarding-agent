"""External benchmark cases from the Raha data-cleaning datasets (dirty and clean pairs).

Source: https://github.com/BigDaMa/raha (Apache-2.0), datasets hospital, flights, and beers,
downloaded at a pinned commit; nothing from it is committed to this repository.

``python -m evals.benchmark`` (``make benchmark-cases``) writes ``evals/benchmark_cases/raha_<name>/``
with ``messy.csv`` (the dirty file as published), ``clean.csv`` (the clean file with the target
column names), and an ``answer_key.json`` that ``evals.run_evals`` reads like the generated cases.
The datasets have no typed defect labels, so the answer keys list no defects; mapping accuracy,
build success, and cell accuracy are what these cases measure. ``baseline_cells`` records how
many cells of the dirty file already equal the clean one, so cleaning can be compared with doing
nothing.
"""  # noqa: E501

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from datagen.schemas import column_names, key_column, load_schema
from evals.scoring import normalize_cell

RAHA_REPO = "https://github.com/BigDaMa/raha"
RAHA_COMMIT = "7be1334b8c7bbdac3f47ef514fb3e1e8c5fc181c"
DATASETS = ("hospital", "flights", "beers")


def raw_url(dataset: str, version: str) -> str:
    return (
        f"https://raw.githubusercontent.com/BigDaMa/raha/{RAHA_COMMIT}/datasets/"
        f"{dataset}/{version}.csv"
    )


def _rows(text: str) -> tuple[list[str], list[list[str]]]:
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    return header, list(reader)


def build_case(dataset: str, dirty_text: str, clean_text: str, out: Path) -> dict[str, Any]:
    """Write one case; the files are row- and column-aligned, so the mapping is positional."""
    schema = load_schema(dataset)
    targets = column_names(schema)
    types = {c["name"]: c["type"] for c in schema["columns"]}
    dirty_header, dirty = _rows(dirty_text)
    clean_header, clean = _rows(clean_text)
    if not (len(dirty_header) == len(clean_header) == len(targets)):
        raise ValueError(f"{dataset}: dirty, clean, and target columns differ in number")
    if len(dirty) != len(clean):
        raise ValueError(f"{dataset}: dirty and clean files differ in rows")

    case_dir = out / f"raha_{dataset}"
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "messy.csv").write_text(dirty_text, encoding="utf-8")
    with (case_dir / "clean.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(targets)
        writer.writerows(clean)

    same = sum(
        normalize_cell(d, types[t]) == normalize_cell(c, types[t])
        for drow, crow in zip(dirty, clean, strict=True)
        for d, c, t in zip(drow, crow, targets, strict=True)
    )
    key = {
        "case_id": f"raha_{dataset}",
        "messy_file": "messy.csv",
        "clean_file": "clean.csv",
        "table": dataset,
        "seed": 0,
        "corruptions": ["raha"],
        "names_changed": dirty_header != targets,
        "key_column": key_column(schema),
        "clean_rows": len(clean),
        "messy_rows": len(dirty),
        "mappings": [
            {"source_column": s, "target_column": t}
            for s, t in zip(dirty_header, targets, strict=True)
        ],
        "unmapped_source_columns": [],
        "missing_target_columns": [],
        "defects": [],
        "baseline_cells": [same, len(clean) * len(targets)],
        "source": f"{RAHA_REPO} @ {RAHA_COMMIT} (Apache-2.0), datasets/{dataset}",
    }
    (case_dir / "answer_key.json").write_text(json.dumps(key, indent=2) + "\n", encoding="utf-8")
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download the Raha benchmark cases.")
    parser.add_argument("--out", type=Path, default=Path("evals/benchmark_cases"))
    args = parser.parse_args(argv)
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            for dataset in DATASETS:
                texts = []
                for version in ("dirty", "clean"):
                    response = client.get(raw_url(dataset, version))
                    response.raise_for_status()
                    texts.append(response.text)
                key = build_case(dataset, *texts, args.out)
                done, total = key["baseline_cells"]
                print(
                    f"raha_{dataset}: {key['messy_rows']} rows, {total // key['messy_rows']} "
                    f"columns, {done / total:.1%} of cells already clean"
                )
    except httpx.HTTPError as exc:
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
