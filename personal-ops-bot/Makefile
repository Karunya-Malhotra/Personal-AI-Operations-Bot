# Tabs matter in Makefiles; .RECIPEPREFIX avoids that entire class of paper cut.
.RECIPEPREFIX := >
.DEFAULT_GOAL := help
.PHONY: help install up down logs migrate revision cli api test lint typecheck contracts check reset

COMPOSE := docker compose -f docker/compose.dev.yml

help:  ## Show available targets
> @grep -E "^[a-zA-Z_-]+:.*?## " $(MAKEFILE_LIST) | awk -F":.*?## " "{printf \"  %-12s %s\\n\", \$$1, \$$2}"

install:  ## Install the package and dev tooling (editable)
> pip install -e ".[dev]"

up:  ## Start PostgreSQL and wait until it is accepting connections
> $(COMPOSE) up -d --wait

down:  ## Stop PostgreSQL (keeps the volume)
> $(COMPOSE) down

reset:  ## Destroy the dev database and its volume. Dev only.
> $(COMPOSE) down -v

logs:  ## Tail PostgreSQL logs
> $(COMPOSE) logs -f postgres

migrate:  ## Apply migrations (also stamps a new database with APP_ENV)
> alembic upgrade head

revision:  ## Autogenerate a migration. ALWAYS review the output by hand. m="message"
> alembic revision --autogenerate -m "$(m)"

cli:  ## Start the interactive CLI
> aiops chat

api:  ## Run the API with reload
> uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000

test:  ## Run tests (integration tests need Docker)
> pytest -q

lint:  ## ruff
> ruff check app tests alembic
> ruff format --check app tests

typecheck:  ## mypy
> mypy app

contracts:  ## Enforce the dependency layering
> lint-imports

check: lint typecheck contracts test  ## Everything CI runs
