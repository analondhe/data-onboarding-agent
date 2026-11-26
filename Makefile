.PHONY: seed test lint format onboard eval eval-smoke model

UV ?= uv
SEED ?= 42
CUSTOMERS ?= 10000
CASE_ROWS ?= 2000

## Generate clean Acme Cloud tables into data/clean/ and 30 corrupted eval cases into evals/cases/
seed:
	$(UV) run python -m datagen.generate --seed $(SEED) --customers $(CUSTOMERS) --out data/clean
	rm -rf evals/cases
	$(UV) run python -m evals.corrupt all --seed $(SEED) --rows $(CASE_ROWS) --clean-dir data/clean --out evals/cases

## Profile and map one file with the configured model: make onboard FILE=x.csv SCHEMA=customers
onboard:
	@test -n "$(FILE)" -a -n "$(SCHEMA)" || (echo "usage: make onboard FILE=path.csv SCHEMA=customers" && exit 2)
	$(UV) run python -m agent.cli onboard --file "$(FILE)" --schema "$(SCHEMA)"

## Build the local Ollama model with a 16k context window (see ollama/qwen3-8b-16k.Modelfile)
model:
	ollama create qwen3-8b-16k -f ollama/qwen3-8b-16k.Modelfile

## Score the configured model on every case in evals/cases/; writes evals/results/<time>_<model>.md
eval:
	$(UV) run python -m evals.run_evals $(EVAL_ARGS)

## CI smoke test: 5 cases with FakeLLM answering from the answer keys; fails unless perfect
eval-smoke:
	$(UV) run python -m evals.run_evals --fake --limit 5 --results-dir data/eval-smoke

## Run all tests; tools/ must stay at 80% line coverage or above
test:
	$(UV) run pytest --cov=tools --cov-report=term-missing --cov-fail-under=80

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .
