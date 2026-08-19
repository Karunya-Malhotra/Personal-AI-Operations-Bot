"""The guard against a real PostgreSQL.

Only one integration test for this, on purpose: the decision logic is covered
exhaustively in tests/unit/test_guard.py without a container. What this adds is
the part unit tests cannot check -- that `read_stamp` returns what migration
0001 actually wrote, and that a missing table is tolerated rather than raising.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.environment import Environment
from app.core.errors import EnvironmentMismatchError
from app.db.guard import assert_database_environment, read_stamp

pytestmark = pytest.mark.integration


# `postgres_dsn` (a freshly created, empty database) comes from tests/conftest.py.


async def test_missing_table_reads_as_no_stamp(postgres_dsn: str) -> None:
    engine = create_async_engine(postgres_dsn)
    try:
        async with engine.connect() as conn:
            assert await read_stamp(conn) is None
    finally:
        await engine.dispose()


async def test_stamp_roundtrip_and_mismatch(postgres_dsn: str) -> None:
    engine = create_async_engine(postgres_dsn)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS system_settings ("
                    "  key VARCHAR(64) PRIMARY KEY,"
                    "  value VARCHAR(256) NOT NULL,"
                    "  updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO system_settings(key, value) VALUES ('environment','prod') "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
                )
            )

        async with engine.connect() as conn:
            assert await read_stamp(conn) == "prod"
            await assert_database_environment(conn, expected=Environment.PROD, host="test")
            with pytest.raises(EnvironmentMismatchError):
                await assert_database_environment(conn, expected=Environment.DEV, host="test")
    finally:
        await engine.dispose()
