# Tabs matter in Makefiles; .RECIPEPREFIX avoids that entire class of paper cut.
.RECIPEPREFIX := >
.DEFAULT_GOAL := help
.PHONY: help install lock up down logs migrate revision cli api test lint typecheck contracts check reset

COMPOSE := docker compose -f docker/compose.dev.yml

# Every target runs through the locked dependency set, so `make test` on a
# fresh clone resolves to the same versions as `make test` in CI -- with no
# "did you activate the venv?" step in between. `--locked` fails loudly if
# uv.lock has drifted from pyproject.toml rather than silently re-resolving,
# which is the property that makes the lock mean anything.
RUN := uv run --locked --extra dev

help:  ## Show available targets
> @grep -E "^[a-zA-Z_-]+:.*?## " $(MAKEFILE_LIST) | awk -F":.*?## " "{printf \"  %-12s %s\\n\", \$$1, \$$2}"

install:  ## Create .venv with the exact locked dependency set
> uv sync --locked --extra dev

lock:  ## Re-resolve after editing pyproject.toml dependencies. Commit uv.lock.
> uv lock

up:  ## Start PostgreSQL and wait until it is accepting connections
> $(COMPOSE) up -d --wait

down:  ## Stop PostgreSQL (keeps the volume)
> $(COMPOSE) down

reset:  ## Destroy the dev database and its volume. Dev only.
> $(COMPOSE) down -v

logs:  ## Tail PostgreSQL logs
> $(COMPOSE) logs -f postgres

migrate:  ## Apply migrations (also stamps a new database with APP_ENV)
> $(RUN) alembic upgrade head

revision:  ## Autogenerate a migration. ALWAYS review the output by hand. m="message"
> $(RUN) alembic revision --autogenerate -m "$(m)"

cli:  ## Start the interactive CLI
> $(RUN) aiops chat

api:  ## Run the API with reload
> $(RUN) uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000

test:  ## Run tests (integration tests need Docker)
> $(RUN) pytest -q

lint:  ## ruff
> $(RUN) ruff check app tests alembic
> $(RUN) ruff format --check app tests

typecheck:  ## mypy
> $(RUN) mypy app

contracts:  ## Enforce the dependency layering
> $(RUN) lint-imports

check: lint typecheck contracts test  ## Everything CI runs
