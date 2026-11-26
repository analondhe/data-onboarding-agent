.PHONY: seed test lint format

UV ?= uv
SEED ?= 42
CUSTOMERS ?= 10000
CASE_ROWS ?= 2000

## Generate clean Acme Cloud tables into data/clean/ and 30 corrupted eval cases into evals/cases/
seed:
	$(UV) run python -m datagen.generate --seed $(SEED) --customers $(CUSTOMERS) --out data/clean
	rm -rf evals/cases
	$(UV) run python -m evals.corrupt all --seed $(SEED) --rows $(CASE_ROWS) --clean-dir data/clean --out evals/cases

## Run all tests; tools/ must stay at 80% line coverage or above
test:
	$(UV) run pytest --cov=tools --cov-report=term-missing --cov-fail-under=80

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .
