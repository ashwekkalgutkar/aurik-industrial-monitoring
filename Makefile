.PHONY: install test lint typecheck check seed compose-up compose-down run-api run-worker

install:
	pip install -e ".[dev]"

test:
	pytest -v tests/

lint:
	ruff check .
	ruff format --check .

typecheck:
	mypy app/

check: lint typecheck test

seed:
	python -m app.cli seed

run-api:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

run-worker:
	python -m app.workers.runner

compose-up:
	docker compose up --build

compose-down:
	docker compose down -v
