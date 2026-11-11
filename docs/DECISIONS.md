# Decisions

One line per choice the spec left open.

## Milestone 1

- **Tooling:** `uv` installed with Homebrew; dependencies locked in `uv.lock`; the project is not packaged (`tool.uv.package = false`), and modules are imported from the repo root.
- **Added dependency:** `pyyaml` reads `schemas/*.yml`. Approved before work started.
- **Parquet:** written by DuckDB (`COPY ... TO ... (FORMAT parquet)`) with column types taken from the schema YAML, so `pyarrow`/`pandas` are not needed.
- **Fixed reference date:** generated dates are anchored to 2025-06-30 instead of today, so the same seed gives the same data on any day.
- **Determinism scope:** the same seed and dependency versions give byte-identical CSV, Parquet, and answer keys (verified by running `make seed` twice). Tests compare CSV bytes and Parquet contents.
- **Required columns:** every column is `required: true` except `country`, `canceled_at`, and `paid_at`. Amount, plan, status, and seats columns have no meaning when empty, even though §7.1 does not say "Required".
- **Clean nulls:** about 2% of customers have no `country`, so `null_variants` has real nulls to render in every table.
- **Volumes:** about 1.2 subscriptions per customer and about 4.6 invoices per subscription (invoices stop at the reference date or the cancellation date).
- **Date ambiguity:** `mixed_date_formats` always uses US `MM/DD/YYYY`, plus at most one unambiguous format (`DD Mon YYYY`, `YYYYMMDD`, `Month DD, YYYY`). Slash dates are always month first.
- **Defect vocabulary:** answer keys use the fixed `DefectType` enum in `evals/corrupt.py`: `duplicate_rows`, `null_variants`, `type_noise`, `value_drift`, `mixed_date_formats`, `merged_column`, `missing_required_column`. The agent must report defects in this vocabulary.
- **Defects vs. mapping challenges:** `rename_columns`, `cryptic_names`, and `extra_columns` are mapping problems, not defects. Extra columns go in `unmapped_source_columns`, and the renames show up in `mappings`.
- **Merged columns:** one source column maps to two targets, and each mapping entry has `merged: {separator, part}`. Pairs come from a fixed list of plausible pairs per table (for example `plan`+`status`, `invoice_date`+`due_date`).
- **Column claiming:** each value-level corruption, merge, and drop takes a different column, so every defect's `affected_rows` is exact. If a planned extra corruption has no free column left, it is dropped from the case.
- **Duplicates:** about half of the duplicate rows differ from the original only by casing or whitespace in a free-text column (IDs included). `affected_rows` counts the extra rows.
- **Type noise:** about 2% of noised cells become unparseable junk (`N/A`, `TBD`, …), counted in `details.unparseable_rows`. In required columns these rows should end up rejected.
- **Case layout:** `evals/cases/<case_id>/` holds `messy.csv`, `clean.csv` (the sampled clean rows, used as truth for cell accuracy), and `answer_key.json`.
- **Case plan:** each table's 10 cases cycle through all 10 corruption types as the primary one, plus 0–3 random extras, so every type appears at least once per table.
- **Makefile:** only targets that work today (`seed`, `test`, `lint`, `format`). `up`, `down`, `eval`, `eval-smoke`, and `onboard` are added in the milestones that implement them.
