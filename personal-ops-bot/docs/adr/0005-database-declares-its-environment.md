# ADR 0005 — The database declares which environment it belongs to

Status: accepted (v0.3 §G.2)

## Context
From M2 there are two PostgreSQL instances, both reached by a `DATABASE_URL`.
A stale shell or a copied `.env` points a laptop at production, and the failure
is silent: development writes into the real ledger.

## Decision
Migration 0001 writes `system_settings('environment', <APP_ENV at migration
time>)`. Both `bootstrap.Container.startup()` and `alembic/env.py` refuse to
proceed when the stamp disagrees with `APP_ENV`, in either direction. An
unstamped database is also refused.

## Rationale
Configuration cannot guard against misconfiguration. The stamp travels with the
data, so a restored dump keeps its identity and a copy-pasted connection string
cannot lie about it.

## Consequence
A restored production dump used for local debugging must be re-stamped
deliberately — which is the intended friction.
