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

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .
