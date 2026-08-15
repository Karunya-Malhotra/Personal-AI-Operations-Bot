# ADR 0002 — PostgreSQL only; no SQLite path

Status: accepted (v0.2 D2)

## Context
A SQLite path for local development was considered.

## Decision
PostgreSQL everywhere, run locally via Docker.

## Rationale
The design depends on pgvector, JSONB operators, `tsvector` FTS, `TEXT[]`,
partial indexes, and `FOR UPDATE SKIP LOCKED`. Dual support means two schemas,
two query dialects, and a class of bug that only appears in production.

## Consequence
Docker is required for local development. `docker compose up` is the cost.
