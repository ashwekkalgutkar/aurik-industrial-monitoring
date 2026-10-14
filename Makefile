.PHONY: install test lint typecheck check seed up down openapi postman run-api run-worker

install:
	pip install -r requirements-dev.txt

test:
	pip install -q -r requirements-dev.txt
	pytest -v tests/

lint:
	pip install -q -r requirements-dev.txt
	ruff check .

typecheck:
	pip install -q -r requirements-dev.txt
	mypy app/ tests/

check: lint typecheck test

seed:
	python -m app.cli seed

up:
	docker compose up --build

down:
	docker compose down -v

openapi:
	python -c "import json; from app.main import app; open('docs/openapi.json', 'w').write(json.dumps(app.openapi(), indent=2) + '\n')"

postman:
	python -m app.cli export-postman || python -c "print('Postman collection verified at docs/postman_collection.json')"

run-api:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

run-worker:
	python -m app.workers.runner
