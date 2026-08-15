# Personal Ops Bot

A personal AI assistant whose interface is chat and whose system of record is
PostgreSQL. See `docs/ARCHITECTURE.md` for the design and `docs/adr/` for the
decisions behind it.

**Current milestone: M1A — Foundation.** No agent, no tools, no LLM yet.
What works: configuration, structured logging, migrations, the dev/prod
database guard, health endpoints, and an echo CLI.

## Quickstart

```bash
cp .env.example .env
make up        # start PostgreSQL
make install   # install the package and dev tooling
make migrate   # create system_settings and stamp the database as 'dev'
make cli       # an echo REPL, with boot checks
make api       # http://127.0.0.1:8000/health
make check     # lint + types + import contracts + tests
```

## Layout

```
app/core/           pure kernel: errors, Clock, Environment. No frameworks.
app/config/         Settings (pydantic-settings). Read only by bootstrap.
app/observability/  structlog setup, correlation ids, redaction.
app/db/             engine, session scope, models, environment guard.
app/api/            FastAPI routers.
app/bootstrap.py    composition root; boot-time invariants.
app/main.py         api entrypoint.
app/cli.py          cli entrypoint.
```

Dependencies point inward only. `lint-imports` enforces it in CI.
