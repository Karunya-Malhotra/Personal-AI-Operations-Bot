"""Alembic environment.

Two things here are load-bearing rather than boilerplate.

1. **The guard runs before migrations apply.** `bootstrap` protects the running
   application, but migrations run outside it -- so `alembic upgrade head` with
   the wrong DATABASE_URL would alter the wrong database's schema before the
   app ever got a chance to refuse. Checking here closes that window. On a
   brand-new database there is no stamp yet, so we skip the check and stamp it
   in migration 0001.

2. **`compare_type=True` and `compare_server_default=True`.** Without these,
   autogenerate misses column type changes entirely, which produces migrations
   that appear to succeed and leave the schema wrong.

`--autogenerate` output is still reviewed by hand every time. It does not see
index changes on expressions, enum alterations, or CHECK constraint edits --
all three of which this schema uses from M1C.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.engine import Connection
from sqlalchemy import pool

from app.config.settings import Settings
from app.db.guard import evaluate_stamp, read_stamp
from app.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = Settings()
config.set_main_option("sqlalchemy.url", settings.database_url.get_secret_value())

target_metadata = Base.metadata


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        stamp = await read_stamp(connection)
        if stamp is not None:
            evaluate_stamp(
                expected=settings.app_env,
                found=stamp,
                host=settings.database_host,
            )
        await connection.run_sync(_run_migrations)
        await connection.commit()

    await connectable.dispose()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
