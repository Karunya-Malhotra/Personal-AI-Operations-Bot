"""Shared fixtures.

Note what is *not* here: no global settings, no shared engine, no autouse
database fixture. Tests construct what they need. That is a direct consequence
of bootstrap having no module-level globals -- it is what makes it cheap.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config.settings import Settings
from app.core.environment import Environment


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove APP_* / DATABASE_* from the environment so Settings sees only defaults.

    Without this, running tests on a machine with a real `.env` would silently
    test that machine's configuration instead of the code.
    """
    for key in list(os.environ):
        if key.upper().startswith(("APP_", "DATABASE_", "DB_", "LOG_", "API_")):
            monkeypatch.delenv(key, raising=False)
    # model_config is a TypedDict, not an object: setitem, not setattr. This
    # stops Settings() from reading a developer's real .env during tests.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    yield


@pytest.fixture
def dev_settings(clean_env: None) -> Settings:
    return Settings(
        app_env=Environment.DEV,
        database_url="postgresql+asyncpg://u:p@localhost:5432/aiops_test",  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Real PostgreSQL for integration tests.
#
# Two ways to get one, in priority order:
#
#   1. `AIOPS_TEST_DATABASE_URL` pointing at a server that already exists. This
#      is what a developer with `make up` running uses, and what CI uses when
#      Postgres is a service container. It is also the only option in
#      environments where Docker is unavailable.
#   2. testcontainers, which starts one. The original behaviour, kept as the
#      zero-configuration default.
#
# Either way each module gets a **freshly created database**, not a shared one.
# That is not tidiness: `test_missing_table_reads_as_no_stamp` asserts that
# `system_settings` does not exist, which is only true of a database nothing has
# migrated yet. Sharing one database between modules would make that test pass
# or fail depending on which other test ran first.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_server_dsn() -> Iterator[str]:
    """DSN of a running PostgreSQL server. Does not name a usable database."""
    configured = os.environ.get("AIOPS_TEST_DATABASE_URL")
    if configured:
        yield configured
        return
    testcontainers = pytest.importorskip("testcontainers.postgres")
    with testcontainers.PostgresContainer("pgvector/pgvector:pg16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql+asyncpg://")


@pytest.fixture(scope="module")
async def postgres_dsn(postgres_server_dsn: str) -> Iterator[str]:
    """A brand-new empty database, dropped when the module finishes."""
    name = f"aiops_test_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(postgres_server_dsn, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()

    base, _, _ = postgres_server_dsn.rpartition("/")
    yield f"{base}/{name}"

    admin = create_async_engine(postgres_server_dsn, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    finally:
        await admin.dispose()
