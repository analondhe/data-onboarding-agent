# Data Onboarding Agent: Project Spec

This file is the source of truth for the project. Read all of it before writing code.

## 1. What we are building

An agent that takes messy customer data files and produces clean, tested tables in a target schema. It profiles the files, proposes a column mapping with a confidence score per column, sends uncertain mappings to a human for review, generates dbt models and tests, runs them, and reports the result.

The framing is a forward deployed engineering engagement. The fictional customer is **Acme Cloud**, a B2B SaaS company migrating customers off a legacy billing system. Each migration currently takes an analyst days of manual column mapping and cleanup.

One **run** processes one customer's files through these states:

`created` → `profiling` → `mapping` → `awaiting_review` → `building` → `done` | `failed`

## 2. Hard constraints

- **Zero cost.** Everything runs locally. No paid APIs or cloud services are required to build, test, or run the project.
- **One command to run.** `make up` starts the whole stack with Docker Compose.
- **Swappable model.** The model is reached through an OpenAI-compatible endpoint set by environment variables. The default is a local Ollama model. No code may assume a specific model or provider.
- **No network in tests.** Tests use a fake model (see section 8) and local files only.
- **No real personal data.** All data is generated or public.

## 3. How to work on this project

1. Build **one milestone at a time**, in the order in section 13. Do not start the next milestone until I approve.
2. At the end of each milestone, stop and give me: what was built, how to run it, test results, and anything you were unsure about.
3. Every milestone ships with tests. `make test` and `make lint` must pass before a milestone is done.
4. Do not add a dependency that is not listed in section 4 without asking first.
5. If this spec is ambiguous or seems wrong, ask before guessing. If you must choose, record the choice in `docs/DECISIONS.md` with a one-line reason.
6. Prefer small, readable modules over abstractions. No agent frameworks (no LangChain, CrewAI, or similar). The tool-calling loop is plain Python.
7. Commit at the end of each milestone with a clear message. Never commit secrets, `.env`, or files under `data/runs/`.

## 4. Tech stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.12 |
| Packaging | `uv` with a lock file (fall back to `pip-tools` if `uv` is unavailable) |
| API | FastAPI, Pydantic v2, Uvicorn |
| Database (app state) | Postgres 16 in Compose; SQLite for tests and CLI use. SQLAlchemy 2 and Alembic |
| Warehouse | DuckDB, one file per run |
| Transformations | dbt-core with dbt-duckdb |
| Model client | `openai` Python SDK pointed at `LLM_BASE_URL` |
| Local model server | Ollama (Compose service) |
| Tools protocol | Official MCP Python SDK |
| Tracing and eval logging | MLflow, self-hosted |
| SQL parsing | `sqlglot` |
| Data generation | `Faker` |
| Web | Next.js (App Router), TypeScript, Tailwind |
| Tests and lint | `pytest`, `ruff` |
| CI | GitHub Actions |

## 5. Architecture

Seven components in six containers.

| Component | Responsibility | Port |
| --- | --- | --- |
| `api` | Run lifecycle, uploads, review decisions, starts agent jobs | 8000 |
| `agent` | Tool-calling loop; runs as a background worker in the API process | n/a |
| `tools` | MCP server exposing profiling, schema, transform, and dbt tools | 8100 |
| `web` | Upload page, review screen, run report | 3000 |
| `postgres` | Run state, profiles, mappings, defects, audit log | 5432 |
| `ollama` | Local model with an OpenAI-compatible endpoint | 11434 |
| `mlflow` | Traces and eval results | 5000 |

Rules:

- The web app talks only to the API.
- The agent calls the model for decisions and the tools for every data operation. The model never sees a full file: only profiles and at most 20 sample rows per call.
- Every tool is deterministic Python over DuckDB. Tools are plain functions first, then wrapped by the MCP server. The agent can call them in-process (CLI and tests) or over MCP (Compose).
- DuckDB lives at `data/runs/<run_id>/warehouse.duckdb`. dbt runs as a subprocess.

Environment variables (all listed in `.env.example`):

```
LLM_BASE_URL=http://ollama:11434/v1
LLM_MODEL=
LLM_API_KEY=not-needed-for-ollama
DATABASE_URL=postgresql+psycopg://app:app@postgres:5432/app
MLFLOW_TRACKING_URI=http://mlflow:5000
DATA_DIR=./data
MAX_UPLOAD_MB=1024
REVIEW_CONFIDENCE_THRESHOLD=0.8
AGENT_MAX_STEPS=25
BUILD_MAX_REPAIRS=3
```

## 6. Repo layout

```text
data-onboarding-agent/
  PROJECT_SPEC.md
  README.md
  docker-compose.yml
  Makefile                 # up, down, test, lint, seed, eval, onboard
  .env.example
  pyproject.toml
  schemas/                 # target schema YAML files
  api/
    main.py                # FastAPI routes
    models.py              # SQLAlchemy tables
    worker.py              # run state machine
    migrations/            # Alembic
  agent/
    loop.py                # tool-calling loop
    phases.py              # profile, map, build
    prompts/               # one system prompt per phase
    llm.py                 # OpenAI-compatible client and FakeLLM
  tools/
    profiling.py
    transforms.py
    dbt_runner.py
    server.py              # MCP server wrapping the functions above
  dbt_project/
    templates/             # Jinja templates for models and schema.yml
  datagen/
    generate.py            # clean Acme Cloud data
  evals/
    corrupt.py             # corruption script and answer keys
    run_evals.py           # harness and scoring
    cases/                 # generated (gitignored)
    results/               # scorecards
  web/                     # Next.js app
  tests/
  docs/
    DECISIONS.md
    runbook.md
  data/runs/               # per-run files (gitignored)
  .github/workflows/ci.yml
```

## 7. Data

### 7.1 Clean source data (`datagen/generate.py`)

Generate three related tables for Acme Cloud with `Faker`. The script takes `--seed` and `--customers` (default 10,000) and is fully deterministic for a given seed. Write Parquet and CSV to `data/clean/`.

**customers**

| Column | Type | Rules |
| --- | --- | --- |
| `customer_id` | varchar | Unique, format `CUS-000001` |
| `company_name` | varchar | Required |
| `contact_email` | varchar | Required, valid email |
| `country` | varchar | ISO 3166-1 alpha-2 |
| `signup_date` | date | Required |
| `plan` | varchar | One of `free`, `pro`, `enterprise` |
| `is_active` | boolean | Required |

**subscriptions** (about 1.2 per customer)

| Column | Type | Rules |
| --- | --- | --- |
| `subscription_id` | varchar | Unique, format `SUB-000001` |
| `customer_id` | varchar | Must exist in customers |
| `plan` | varchar | One of `free`, `pro`, `enterprise` |
| `status` | varchar | One of `trialing`, `active`, `past_due`, `canceled` |
| `started_at` | date | Required, on or after the customer's `signup_date` |
| `canceled_at` | date | Null unless status is `canceled` |
| `monthly_amount_usd` | decimal(10,2) | Zero or more |
| `seats` | integer | One or more |

**invoices** (about 5 per subscription)

| Column | Type | Rules |
| --- | --- | --- |
| `invoice_id` | varchar | Unique, format `INV-0000001` |
| `customer_id` | varchar | Must exist in customers |
| `subscription_id` | varchar | Must exist in subscriptions |
| `invoice_date` | date | Required |
| `due_date` | date | On or after `invoice_date` |
| `amount_usd` | decimal(10,2) | Zero or more |
| `status` | varchar | One of `open`, `paid`, `void`, `uncollectible` |
| `paid_at` | date | Null unless status is `paid` |

### 7.2 Target schemas (`schemas/*.yml`)

One YAML file per target table, matching the tables above. Each column has `name`, `type`, `required`, `unique`, `allowed_values` (optional), and a one-line `description`. The description is what the model matches source columns against, so write it carefully.

```yaml
table: customers
columns:
  - name: customer_id
    type: varchar
    required: true
    unique: true
    description: Stable identifier for the customer
  - name: signup_date
    type: date
    required: true
    description: Date the customer first created an account
  - name: plan
    type: varchar
    allowed_values: [free, pro, enterprise]
    description: Current subscription tier
```

### 7.3 Corruption script (`evals/corrupt.py`)

Takes a clean table, its target schema, a seed, and a list of corruptions. Writes a messy CSV and an `answer_key.json` into `evals/cases/<case_id>/`.

| Corruption | Example |
| --- | --- |
| `rename_columns` | `signup_date` → `Sign Up Dt` |
| `cryptic_names` | `plan` → `pln_cd` |
| `mixed_date_formats` | `2024-03-01` and `03/01/2024` in one column |
| `type_noise` | `$1,200.00`, `1200`, `N/A` |
| `duplicates` | 3% of rows repeated, some with different casing or whitespace |
| `null_variants` | `NULL`, `-`, empty string, `n/a` |
| `value_drift` | `Pro`, `PRO`, `professional` |
| `merge_columns` | Two columns merged into one |
| `extra_columns` | Unrelated columns added |
| `drop_required` | A required column removed |

`answer_key.json` records: the true source-to-target mapping for every column, columns that should stay unmapped, every injected defect with its type, column and affected row count, and the path to the clean table for cell-level comparison.

`make seed` generates the clean data and 30 cases: 10 per table, with one to four corruptions each, at 2,000 rows per case.

### 7.4 Other data (later milestones)

- External benchmark: the Raha data-cleaning datasets (dirty and clean pairs). Added in milestone 8. Confirm the repository and license before use.
- Live source: NYC Open Data, "311 Service Requests from 2020 to Present" (dataset id `erm2-nwe9`), through its public API. Used in milestone 7 for recurring loads and in the demo. It has no answer key, so it is never used for scoring.

## 8. Agent

### 8.1 Loop (`agent/loop.py`)

Send messages and tool definitions to the model. Execute any tool calls it returns, append the results, and repeat until it calls a `submit_*` tool or reaches `AGENT_MAX_STEPS`. Reaching the step limit ends the run cleanly as `failed` with a clear reason.

### 8.2 Phases

Each phase has its own system prompt and tool subset, so the model never sees more than six tools.

| Phase | Tools | Ends when |
| --- | --- | --- |
| Profile | `list_files`, `profile_file`, `sample_rows`, `detect_patterns` | Every file has profiles stored |
| Map | `get_target_schema`, `get_column_profile`, `sample_rows`, `test_transform`, `submit_mappings` | `submit_mappings` passes validation |
| Build | `update_transform`, `run_dbt`, `get_dbt_errors`, `submit_build` | `dbt build` passes, or `BUILD_MAX_REPAIRS` attempts are used |

The Profile phase can run without the model: call the profiling functions directly for every file. Only use the model there if a later need appears.

### 8.3 Tool contracts

| Tool | Input | Output |
| --- | --- | --- |
| `list_files` | `run_id` | File ids, names, row counts |
| `profile_file` | `file_id` | Per column: inferred type, null rate, distinct count, min, max, top 5 values |
| `sample_rows` | `file_id`, `n` (max 20), optional `where` | Rows as JSON |
| `detect_patterns` | `file_id`, `column` | Shape counts, for example `{"YYYY-MM-DD": 9120, "MM/DD/YYYY": 880}` |
| `get_target_schema` | `schema_name` | The YAML target as JSON |
| `get_column_profile` | `file_id`, `column` | The stored profile for one column |
| `test_transform` | `file_id`, `sql_expression`, `target_type` | Success rate of the cast and 5 failing examples |
| `submit_mappings` | List of `{source_column, target_column, transform_sql, confidence, reason}` plus a list of defects | Validation errors, or accepted |
| `update_transform` | `mapping_id`, `transform_sql`, `reason` | Updated mapping |
| `run_dbt` | optional `select` | Pass and fail counts per model and test |
| `get_dbt_errors` | none | Error text and up to 5 failing rows per failed test |
| `submit_build` | `summary` | Ends the Build phase |

### 8.4 Rules

1. `submit_mappings` is validated with Pydantic. Unknown target columns, missing required columns, duplicate targets, and SQL that fails to parse are returned to the model as errors to fix.
2. `transform_sql` must be a single DuckDB `select` expression over one source row (for example `try_strptime(signup_dt, '%m/%d/%Y')::date`). Parse it with `sqlglot` and refuse anything else.
3. Agent-written SQL runs on a read-only DuckDB connection to the raw data.
4. Any mapping with confidence below `REVIEW_CONFIDENCE_THRESHOLD`, and any required target column with no mapping, goes to human review. The rest is auto-approved but still shown.
5. Model timeouts retry 3 times with backoff. Malformed tool calls are returned to the model as an error message, not raised.
6. Every model call and tool call is an MLflow span under one trace per run. Record model name, tokens, and latency.

### 8.5 Fake model (`agent/llm.py`)

Provide a `FakeLLM` with the same interface as the real client. It replays scripted responses, and can be told to time out or return malformed JSON. All unit and integration tests use it.

## 9. App data model (Postgres)

| Table | Key columns |
| --- | --- |
| `runs` | `id`, `customer_name`, `target_schema`, `status`, `run_type`, `contract_id`, `model_name`, `error`, `created_at`, `updated_at` |
| `source_files` | `id`, `run_id`, `filename`, `row_count`, `sha256` |
| `column_profiles` | `id`, `file_id`, `column_name`, `inferred_type`, `null_rate`, `distinct_count`, `sample_values` (json), `patterns` (json) |
| `mappings` | `id`, `run_id`, `file_id`, `source_column`, `target_table`, `target_column`, `transform_sql`, `confidence`, `reason`, `status` (`proposed`, `approved`, `edited`, `rejected`) |
| `defects` | `id`, `run_id`, `file_id`, `column_name`, `defect_type`, `affected_rows`, `proposed_fix`, `status` |
| `review_events` | `id`, `run_id`, `item_type`, `item_id`, `action`, `before` (json), `after` (json), `created_at` |
| `mapping_contracts` | `id`, `customer_name`, `source_name`, `version`, `fingerprint`, `baseline_profile` (json), `created_from_run_id` (milestone 7) |

## 10. dbt generation and build

Most of the dbt project is rendered from templates. The model only writes `transform_sql` expressions.

1. **Load raw.** Load each source file into DuckDB as `raw.<file_stem>` with every column as text.
2. **Render staging models.** A Jinja template turns approved mappings into `stg_<target_table>.sql`: one `select` with each `transform_sql` aliased to its target column.
3. **Render tests.** Generate `schema.yml` from the target schema: `not_null` for required columns, `unique` for keys, `accepted_values` for enumerations.
4. **Quarantine, never drop.** Rows that fail a cast or a required check go to `rejected_<target_table>` with a `reject_reason` column. Duplicates on the key keep one row and send the rest to rejected.
5. **Run.** Run `dbt build` as a subprocess and parse `run_results.json`.
6. **Repair.** On failure, the Build phase agent gets the error and failing rows, updates the affected transform, and reruns, up to `BUILD_MAX_REPAIRS` times. Then the run is `failed` with the last error.
7. **Row accounting.** After a build, assert `rows in = rows loaded + rows rejected` for every file. If it does not hold, the run fails.

A repair that changes an approved mapping is recorded in `review_events` with `action = agent_repair`.

## 11. Evaluation harness (`evals/run_evals.py`)

`make eval` runs every case in `evals/cases/` end to end. Human review is auto-answered from the answer key. Each run is logged to MLflow, tagged with the model name. A scorecard is written to `evals/results/<timestamp>_<model>.md` and `.json`.

| Metric | Definition |
| --- | --- |
| Mapping accuracy | Source columns mapped to the correct target (or correctly left unmapped), divided by all source columns |
| Defect recall | Injected defects flagged, divided by all injected defects |
| Defect precision | Flagged defects that were real, divided by all flagged defects |
| Cell accuracy | Cells in the output table equal to the clean table, joined on the key |
| Escalation rate | Share of mappings sent to human review |
| Bad auto-approvals | Wrong mappings with confidence at or above the threshold |
| Build success | Runs where `dbt build` passed within the repair limit |
| Consistency | Share of cases that pass on all 3 of 3 repeated runs (`--repeats 3`) |

Bad auto-approvals is the number to drive toward zero.

`make eval-smoke` runs 5 cases with `FakeLLM` scripts, for CI.

## 12. Production readiness requirements

These apply from the milestone where the relevant code first appears.

| Area | Requirement | Proof |
| --- | --- | --- |
| Tests | Unit tests for every profiling and transform tool; one integration test from file to passing build | `make test`; coverage of `tools/` at 80% or above |
| CI | Lint, tests, and `make eval-smoke` on every push | `.github/workflows/ci.yml` |
| Safe reruns | Loading the same file twice creates no duplicates; a failed run resumes from its last completed phase | Tests |
| Row accounting | Every input row is loaded or rejected with a reason | Assertion in the build step and a test |
| Model failures | Timeouts, malformed output, and the step limit all end cleanly | Tests using `FakeLLM` |
| Bad input | Empty files, wrong encodings, oversized files, and unsupported types are rejected at upload with a readable message | One test per case |
| SQL safety | Read-only connection; single-expression check | Test that a `drop table` attempt is refused |
| Secrets and config | All settings from environment variables; nothing sensitive committed | `.env.example` and a secret scan in CI |
| Observability | One MLflow trace per run; structured JSON logs with `run_id`; model calls, tokens, and run time in the report | Checked in the integration test |
| Health | `GET /health` checks Postgres, the model endpoint, and the tools server | Compose health checks |
| Reproducibility | Locked dependencies, pinned images, Alembic migrations | Fresh clone runs with `make up` |

## 13. Milestones

Build in this order. Stop after each one.

### Milestone 1: Data and answer keys

- Repo scaffold: `pyproject.toml`, `Makefile`, `ruff`, `pytest`, `.env.example`, `.gitignore`, CI workflow running lint and tests.
- `datagen/generate.py`, the three target schema YAML files, `evals/corrupt.py`.
- **Done when:** `make seed` produces the clean tables and 30 cases with answer keys; the same seed gives identical output; tests cover every corruption type.

### Milestone 2: Profiling and transform tools

- `tools/profiling.py` and `tools/transforms.py` as plain functions over DuckDB, including the SQL safety checks.
- **Done when:** tests show profiles are correct on hand-built fixtures; `test_transform` reports correct success rates; unsafe SQL is refused; coverage of `tools/` is 80% or above.

### Milestone 3: Mapping agent and first scorecard

- `agent/llm.py` (real client and `FakeLLM`), `agent/loop.py`, the Map phase, SQLAlchemy models with SQLite, MLflow tracing to a local file store.
- A CLI: `make onboard FILE=... SCHEMA=...` prints the proposed mappings and defects.
- `evals/run_evals.py` with mapping accuracy, defect recall and precision, escalation rate, and bad auto-approvals.
- **Done when:** the CLI works against a local Ollama model; `make eval` writes a scorecard; `make eval-smoke` passes in CI with `FakeLLM`.

### Milestone 4: dbt build, repair, and row accounting

- `tools/dbt_runner.py`, the Jinja templates, the Build phase, quarantine tables, row accounting.
- Add cell accuracy, build success, and consistency to the evals.
- **Done when:** the CLI takes one messy file to a passing `dbt build` with a report; the integration test passes; rerunning the same file creates no duplicates.

This is the thin end-to-end slice. Milestones 5 onward widen it.

### Milestone 5: Services

- FastAPI endpoints, Postgres with Alembic, the run state machine with resume, the MCP server wrapping the tools, and the full Compose stack with health checks.

| Method and path | Purpose |
| --- | --- |
| `POST /runs` | Create a run |
| `POST /runs/{id}/files` | Upload source files |
| `POST /runs/{id}/start` | Start profiling and mapping |
| `GET /runs/{id}` | Status, phase, step count |
| `GET /runs/{id}/mappings` | Proposed mappings |
| `GET /runs/{id}/defects` | Detected defects |
| `POST /runs/{id}/review` | Submit decisions and start the build |
| `GET /runs/{id}/report` | Final report |
| `GET /health` | Dependency checks |

- **Done when:** `make up` starts everything; a run completes through the API with `curl`; bad uploads are rejected with clear messages.

### Milestone 6: Web app

- **New run:** customer name, schema picker, file drop zone.
- **Review:** one row per source column with sample values, proposed target, transform SQL, confidence, and reason. Low confidence first. Approve, edit, reject. A second tab for defects.
- **Report:** rows in, loaded, and rejected with reasons; dbt test results; link to the MLflow trace; model calls and run time.
- **Done when:** a run can be completed in the browser without touching the terminal.

### Milestone 7: Recurring loads and drift detection

- Save approved mappings as a `mapping_contracts` row with a fingerprint (hash of sorted column names and inferred types) and a baseline profile.
- `POST /contracts/{id}/loads`: profile the new file with no model call, compare with the contract, and either apply the saved mapping or send only the changed columns to the Map phase and human review. Save a new contract version after review.
- Load incrementally, upserting on the unique key, with `loaded_at` and `run_id` on every row.

| Check | Trigger | Action |
| --- | --- | --- |
| Column added | Name not in contract | Agent proposes; human reviews |
| Column missing | Contract column absent | Block if required; otherwise warn |
| Column renamed | One missing, one added, similar values | Agent proposes; human confirms |
| Type or format change | New pattern | Agent proposes a new transform; human reviews |
| New category value | Outside allowed set | Quarantine rows; agent proposes a value mapping |
| Null rate jump | More than 10 points above baseline | Warn; load continues |
| Volume anomaly | Below 50% or above 200% of recent average | Warn; load continues |

- `make pull-311` fetches one day of NYC 311 records and submits them as a recurring load.
- Add drift eval cases, including unchanged batches to measure false alarms.
- **Done when:** an unchanged second file loads with zero model calls; each drift type is detected in tests.

### Milestone 8: Hardening and write-up

- Error analysis on the eval results and fixes, with before and after scorecards kept in `evals/results/`.
- Run the eval suite against a second model through the same endpoint settings and add the comparison.
- Add the external benchmark cases.
- `README.md` as a case study: the customer problem, the approach, results, what broke and how it was fixed, known limits (no authentication, single machine, tested to about 1 GB per file), and how to run it.
- `docs/runbook.md`: start, stop, reset, read a trace, recover a failed run.

## 14. Out of scope

- Merging the same entity across different files
- Streaming ingestion
- Authentication and multi-tenant access
- Cloud deployment

## 15. First task

Start Milestone 1. Before writing code, reply with a short plan: the files you will create, any questions about this spec, and anything in it you would change.
