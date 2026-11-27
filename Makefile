.PHONY: up down logs seed test lint format onboard eval eval-smoke eval-drift benchmark-cases eval-benchmark model web-install web-dev web-build pull-311

UV ?= uv
# Next.js otherwise sends anonymous usage telemetry during builds
export NEXT_TELEMETRY_DISABLED := 1
SEED ?= 42
CUSTOMERS ?= 10000
CASE_ROWS ?= 2000

## Build and start the whole stack (web, api, tools, postgres, ollama, mlflow); waits for health checks
up:
	docker compose up -d --build --wait
	@echo "Web app http://localhost:3000  ·  API http://localhost:8000/docs  ·  MLflow http://localhost:5001  ·  MCP tools http://localhost:8100/mcp"

## Stop the stack (data volumes are kept; `docker compose down -v` deletes them)
down:
	docker compose down

logs:
	docker compose logs -f --tail=100 api tools

## Generate clean Acme Cloud tables into data/clean/, 30 corrupted eval cases into evals/cases/,
## and 33 drift cases into evals/drift_cases/
seed:
	$(UV) run python -m datagen.generate --seed $(SEED) --customers $(CUSTOMERS) --out data/clean
	rm -rf evals/cases evals/drift_cases
	$(UV) run python -m evals.corrupt all --seed $(SEED) --rows $(CASE_ROWS) --clean-dir data/clean --out evals/cases
	$(UV) run python -m evals.drift build --seed $(SEED) --clean-dir data/clean --out evals/drift_cases

## Profile and map one file with the configured model: make onboard FILE=x.csv SCHEMA=customers
onboard:
	@test -n "$(FILE)" -a -n "$(SCHEMA)" || (echo "usage: make onboard FILE=path.csv SCHEMA=customers" && exit 2)
	$(UV) run python -m agent.cli onboard --file "$(FILE)" --schema "$(SCHEMA)"

## Fetch one day of NYC 311 requests and submit it as a recurring load (stack must be running): make pull-311 [DATE=2026-11-25]
pull-311:
	$(UV) run python -m datagen.pull_311 $(if $(DATE),--date $(DATE),)

## Build the local Ollama model with a 16k context window (see ollama/qwen3-8b-16k.Modelfile)
model:
	ollama create qwen3-8b-16k -f ollama/qwen3-8b-16k.Modelfile

## Score the configured model on every case in evals/cases/; writes evals/results/<time>_<model>.md
eval:
	$(UV) run python -m evals.run_evals $(EVAL_ARGS)

## CI smoke test: 5 cases with FakeLLM answering from the answer keys; fails unless perfect
eval-smoke:
	$(UV) run python -m evals.run_evals --fake --limit 5 --results-dir data/eval-smoke

## Recurring loads of every drift case (proposals by the configured model; add EVAL_ARGS=--fake
## for answer-key proposals); writes evals/results/<time>_drift_<model>.md
eval-drift:
	$(UV) run python -m evals.drift run $(EVAL_ARGS)

## Download the Raha benchmark (hospital, flights, beers; Apache-2.0) into evals/benchmark_cases/
benchmark-cases:
	$(UV) run python -m evals.benchmark --out evals/benchmark_cases

## Score the configured model on the Raha benchmark cases; writes evals/results/<time>_raha_<model>.md
eval-benchmark:
	$(UV) run python -m evals.run_evals --cases-dir evals/benchmark_cases --label raha $(EVAL_ARGS)

## Run all tests; tools/ must stay at 80% line coverage or above
test:
	$(UV) run pytest --cov=tools --cov-report=term-missing --cov-fail-under=80

## Lint Python (ruff) and the web app (eslint + TypeScript); run `make web-install` once first
lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	npm --prefix web run lint
	npm --prefix web run typecheck

format:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

## Install the web app's locked npm dependencies into web/node_modules
web-install:
	npm --prefix web ci

## Run the web app with hot reload on :3000 against the API on :8000
web-dev:
	npm --prefix web run dev

## Production build of the web app (also type-checks)
web-build:
	npm --prefix web run build
