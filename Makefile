.PHONY: install lint format types testdb test check up down smoke bench

TEST_DB ?= postgresql://postgres:postgres@127.0.0.1:5433/sentinel_test

install:
	python -m pip install -e ".[dev]"

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

types:
	mypy

testdb:
	docker run --rm --detach --name sentinel-testdb -p 5433:5432 \
	  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=sentinel_test postgres:17-alpine

test:
	SENTINEL_TEST_DATABASE_URL=$(TEST_DB) SENTINEL_REQUIRE_DB=1 pytest --cov

check: lint types test

up:
	docker compose up --build --detach

down:
	docker compose down --volumes

smoke:
	./scripts/smoke.sh

bench:
	python scripts/loadtest.py --events 20000 --processes 4
