# Runbook

How to start, stop, reset, and look after the Data Onboarding Agent on one machine.

## Start

1. Copy `.env.example` to `.env` and set the model. For a hosted model (OpenRouter):

   ```sh
   LLM_BASE_URL=https://openrouter.ai/api/v1
   LLM_API_KEY=sk-or-...
   LLM_MODEL=qwen/qwen3-32b
   ```

   For the local Ollama service in Compose, leave `LLM_BASE_URL` unset, set `LLM_MODEL`, and
   pull the model once: `docker compose exec ollama ollama pull <model>`.
2. `make up`. It builds the images and waits until every service passes its health check.

| Service | Address | Health check |
| --- | --- | --- |
| Web app | http://localhost:3000 | page loads |
| API | http://localhost:8000/docs | `GET /health`: database, model endpoint, tools server |
| MLflow | http://localhost:5001 | `/health` |
| MCP tools | http://localhost:8100/mcp | `/health` |
| Postgres, Ollama | inside Compose only | `pg_isready`, `ollama list` |

`make up` failing on the `api` service almost always means the model is not reachable:
`curl -s localhost:8000/health` names the failing check (for example `model: error: ...`).
An unset `LLM_MODEL` makes the model check fail on purpose.

## Stop

- `make down` stops the containers and keeps all data (runs, sources, traces).
- `make logs` follows the API and tools logs. API lines are JSON with a `run_id` field:
  `docker compose logs api | grep '"run_id": "run-…"'`.

## Reset

Data lives in Docker volumes: `run-data` (warehouses, dbt projects, source tables, uploads),
`pg-data` (runs, mappings, contracts), `mlflow-data` (traces), `ollama-models`.

- Everything: `docker compose down -v`, then `make up`. Migrations recreate the tables.
- Only runs and sources: `docker compose down`, then
  `docker volume rm data-onboarding_run-data data-onboarding_pg-data`, then `make up`.
- Outside Docker (CLI and evals): delete `data/` (keeps nothing) or `data/app.db` and
  `data/runs/` (keeps the clean tables and traces). `make seed` rebuilds the eval cases.

## Read a trace

Every run is traced in MLflow: one trace per `advance` (a run reviewed by a person has a mapping
trace and a build trace).

- From the web app: the run's **Report** tab, "Open in MLflow".
- From the API: `GET /runs/{id}/report` returns `trace_url`.
- In MLflow (http://localhost:5001), experiment `data-onboarding`, Traces tab: filter by the
  `run_id` tag.

The span tree shows `phase:profile:<file>`, `phase:map:<file>` (or `phase:drift-map:<file>`),
`phase:build`, and inside them one `llm` span per model call (model, tokens, latency) and one
`tool:<name>` span per tool call with its arguments and result. A run that failed at the step
limit shows the repeated tool calls that used the steps up.

## Recover a failed run

A failed run keeps everything it finished. On its page the error is shown with a **Resume**
button (`POST /runs/{id}/start`), which continues from the last completed phase.

| Error starts with | Meaning | What to do |
| --- | --- | --- |
| `Interrupted while …` | The API restarted mid-run | Resume |
| `Mapping … failed: Reached the step limit …` | The model used `AGENT_MAX_STEPS` without a valid submission | Resume (it tries again), or raise `AGENT_MAX_STEPS` |
| `… The model did not respond after 4 attempts` / `… The model call failed` | Timeouts, rate limits, or provider errors (retried 3 times), or a bad key or model name | Check the provider and `.env`, then Resume |
| `dbt build failed: …` | A transform still fails after `BUILD_MAX_REPAIRS` repairs | Fix the transform on the Mappings tab, then "Save and rebuild" |
| `Row accounting failed: …` | Rows in ≠ loaded + rejected (should never happen) | Keep the run and report it; check `data/runs/<id>/dbt/dbt_build.log` |
| `Load blocked: Column '…' is missing` | A recurring file lacks a column that fills a required target | Fix the file and load it again, or onboard it as a new source |
| `Internal error: …` | An unexpected exception | Check `make logs` for the traceback |

Files of a run on disk: `data/runs/<run_id>/uploads/` (the uploads),
`warehouse.duckdb` (raw tables, profiles, built models), `dbt/` (rendered project, `target/`,
`dbt_build.log`). Inspect a warehouse read-only (inside Compose the files are in the `run-data`
volume; run this in `docker compose exec api` with `/app/data` as the path):

```sh
uv run python -c "import duckdb; print(duckdb.connect('data/runs/<run_id>/warehouse.duckdb', read_only=True).sql('SELECT * FROM main.rejected_customers LIMIT 20'))"
```

## Recurring sources

- A source's table: `data/sources/<customer>-<source>-<hash>/warehouse.duckdb`, one table named
  like the target, with `loaded_at` and `run_id` on every row.
- A load waiting for review holds nothing back for other sources; review it on its run page.
- To start over with a source, onboard a new file and save it under a new source name. Older
  versions and their loads stay listed on the source page.

## NYC 311

`make pull-311 DATE=YYYY-MM-DD` fetches one day and submits it. The feed lags by a day or two;
pulling a partial day raises a volume warning, and pulling it again later only updates rows.
