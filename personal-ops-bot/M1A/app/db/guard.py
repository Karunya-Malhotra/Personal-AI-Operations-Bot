"""The dev/prod database guard.

The problem, stated concretely: from M2 there are two PostgreSQL instances --
one in Docker on the laptop, one on the always-on host. Both are reached by a
`DATABASE_URL`. A stale shell, a copied `.env`, a `docker compose` run from the
wrong directory, or a half-finished deploy all point the laptop at production.
The failure is silent: development writes test notes into the real ledger, and
nobody notices until the numbers are wrong.

Configuration alone cannot fix this, because configuration is exactly what is
wrong in every one of those scenarios. So the *database* declares its own
identity, and the process refuses to run against a database that disagrees
with it.

Three properties of the design:

  - The stamp is written once, by the first migration, on the host that owns
    the data (see alembic/env.py). It is never updated by application code.
  - The check runs in `bootstrap`, before any session is handed to application
    code. Not in a middleware, not on first query.
  - It runs in *both* directions. `APP_ENV=prod` against a dev database is also
    an error -- that one means a deploy is misconfigured and would otherwise
    quietly serve an empty database.

Note the split below: `evaluate_stamp` is a pure function with no database in
sight, and `read_stamp` is the only thing that touches I/O. The decision is
therefore unit-testable without Postgres, which is why the interesting test
cases (missing stamp, mismatch, unknown value) run in milliseconds in CI while
only one test needs a container.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.environment import Environment
from app.core.errors import ConfigurationError, EnvironmentMismatchError

STAMP_KEY = "environment"


def evaluate_stamp(*, expected: Environment, found: str | None, host: str) -> None:
    """Decide whether this process may use this database. Pure; raises or returns.

    `found is None` means the database has no stamp -- either it predates the
    guard or migrations have never been run against it. We treat that as fatal
    rather than as "probably fine", because an unstamped database is exactly
    what a freshly-restored production dump looks like.
    """
    if found is None:
        raise ConfigurationError(
            f"The database at {host!r} has no {STAMP_KEY!r} stamp. "
            f"Run `make migrate` against it first; migrations write the stamp."
        )

    try:
        found_env = Environment(found)
    except ValueError as exc:
        raise ConfigurationError(
            f"The database at {host!r} is stamped {found!r}, which is not a known "
            f"environment ({[e.value for e in Environment]}). Refusing to start."
        ) from exc

    if found_env is not expected:
        raise EnvironmentMismatchError(expected=expected.value, found=found_env.value, host=host)


async def read_stamp(conn: AsyncConnection) -> str | None:
    """Read the environment stamp, or None if the table or row is absent.

    Tolerates a missing table on purpose: `alembic/env.py` calls this *before*
    applying migrations, at which point on a brand-new database the table does
    not exist yet.
    """
    exists = await conn.scalar(text("SELECT to_regclass('public.system_settings')"))
    if exists is None:
        return None
    value = await conn.scalar(
        text("SELECT value FROM system_settings WHERE key = :key"), {"key": STAMP_KEY}
    )
    return str(value) if value is not None else None


async def assert_database_environment(
    conn: AsyncConnection, *, expected: Environment, host: str
) -> None:
    """Read the stamp and enforce it. The function bootstrap actually calls."""
    evaluate_stamp(expected=expected, found=await read_stamp(conn), host=host)
