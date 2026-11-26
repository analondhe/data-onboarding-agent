# Decisions

One line per choice the spec left open.

## Milestone 1

- **Tooling:** `uv` installed with Homebrew; dependencies locked in `uv.lock`; the project is not packaged (`tool.uv.package = false`), and modules are imported from the repo root.
- **Added dependency:** `pytest-cov` (dev) measures the 80% `tools/` coverage that Milestone 2 requires. Approved by the user.
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

## Milestone 2

- **Added dependencies:** `sqlglot` and `pydantic`, both listed in §4 of the spec. Tool results are Pydantic models, so the agent and the MCP server can serialize them to JSON.
- **Extra module:** `tools/warehouse.py` owns the per-run DuckDB file, loading files into `raw`, and the file registry. Profiling and transforms both depend on it.
- **Tool metadata lives in the warehouse:** `meta.files` and `meta.column_profiles` sit in the run's DuckDB file, so the tools work on DuckDB alone (in-process or over MCP). The app database copies them from Milestone 3 onward.
- **file_id:** the first 16 hex characters of the file's sha256. Loading identical content again, even under another name, returns the existing file and creates no new table.
- **Raw tables:** `raw.<sanitized stem>` (`_2`, `_3`, … if the name is taken), every column text, plus `_row` = 1-based source row number. A source column named `_row` is rejected.
- **Supported inputs (for now):** `.csv`, `.tsv`, `.txt` (delimiter auto-detected, UTF-8) and `.parquet`. Upload limits and encoding handling come with the API in Milestone 5.
- **Nulls:** empty and whitespace-only cells count as null. Text such as `N/A`, `NULL`, `-` is reported separately as `null_like_count` and is left out when inferring types.
- **Type inference:** the type matching the largest share of real values wins (ties go to the more specific type: boolean, integer, decimal, date, timestamp). Below 50%, the column is `varchar`. `type_match_rate` reports the share. Only ISO dates count as `date`; other formats show up in patterns.
- **Patterns:** named shapes first (date formats, numbers, currency, booleans, email, null-like text), else a generic shape (`CUS-000001` → `A-999999`). Leading or trailing whitespace adds `(padded)`. The top 12 shapes are kept and the rest summed under `other shapes`. Slash dates are named `MM/DD/YYYY` or `DD/MM/YYYY` only when some value in the column settles the order; otherwise `NN/NN/YYYY`.
- **sample_rows:** `n` above 20 is capped at 20, not rejected. Rows are picked by `hash(_row)` (spread across the file, the same on every call) and returned in source order. Cells longer than 200 characters are truncated.
- **SQL safety:** model SQL is parsed with sqlglot and must be one expression (no statements, subqueries, table references, `*`, aggregates, window functions, lambdas or parameters). It may only use bare column names from the file, and functions that read files, settings, or the catalog, or that are not deterministic, are refused. The SQL sqlglot regenerates (without comments) is what runs, on a read-only connection with external access disabled. `sample_rows(where=...)` uses the same check.
- **test_transform:** a row counts as input when any referenced column is non-blank. `success_rate = converted / non_null_input`; failures are split into `null_output` (the expression returned NULL) and `cast_failed` (output does not cast to the target type), with up to 5 examples in row order. An expression that errors at runtime (e.g. a strict `::date`) is reported as a tool error suggesting `try_cast`/`try_strptime`.
- **Coverage gate:** `make test` fails if `tools/` line coverage drops below 80%.
