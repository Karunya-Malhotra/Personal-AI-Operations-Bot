"""Async engine and session factory.

Two choices worth explaining:

`expire_on_commit=False`. SQLAlchemy's default expires every loaded object after
commit, so touching an attribute afterwards triggers a lazy refresh. In async
code that raises `MissingGreenlet` instead of quietly doing I/O -- an error that
reads as a framework bug and is actually a design default. Turning it off means
objects stay usable after the transaction closes, which is what a service method
returning an entity needs.

`pool_pre_ping=True`. Postgres, or anything between us and it, will drop idle
connections. Without pre-ping the first query after an idle period fails. This
matters more than usual here: the worker (M2) sits idle between scheduled jobs,
so *every* reminder would hit a stale connection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(
    dsn: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
    connect_timeout_s: float = 5.0,
    echo: bool = False,
) -> AsyncEngine:
    return create_async_engine(
        dsn,
        echo=echo,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        connect_args={"timeout": connect_timeout_s},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One transaction per unit of work: commit on success, roll back on error.

    From M3 this is the boundary that makes the transactional outbox work -- a
    domain mutation, its audit row, and its domain event all commit together or
    not at all.
    """
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
