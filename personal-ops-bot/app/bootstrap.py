"""Composition root: the one place where configuration becomes objects.

Everything the process needs is constructed here and passed down by argument.
Nothing else in `app` reads `Settings`, and nothing else creates an engine.

Why this instead of a DI framework: a DI container solves a problem we do not
have (hundreds of wirings, runtime-selected implementations) and creates one we
would rather avoid (construction order becomes implicit, and "where does this
object come from" stops being greppable). Sixty lines of explicit constructor
calls is clearer and makes `build_container(overrides=...)` in a test trivial.

Why the boot checks live here: the architecture accumulates a list of things
that must be true before the process may serve traffic --

    M1A: the database agrees with APP_ENV
    M1C: every registered tool has a policy declaration (both directions)
    M1C: every tool's declared credentials exist in the grant table
    M1D: every Actor has an entry in SCOPES_BY_ACTOR

-- and all of them are "fail loudly at boot" conditions. `Container.startup()`
is the hook they attach to, so adding one later is a line rather than a design.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.core.clock import Clock, SystemClock
from app.db.engine import create_engine, create_session_factory
from app.db.guard import assert_database_environment
from app.observability.logging import configure_logging, get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Container:
    """The assembled application. Passed down; never a global."""

    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock

    async def startup(self) -> None:
        """Run every boot-time invariant. Raises ConfigurationError to abort."""
        async with self.engine.connect() as conn:
            await assert_database_environment(
                conn,
                expected=self.settings.app_env,
                host=self.settings.database_host,
            )
        log.info(
            "boot.checks_passed",
            app_env=self.settings.app_env.value,
            database_host=self.settings.database_host,
        )

    async def shutdown(self) -> None:
        await self.engine.dispose()


def build_container(settings: Settings | None = None, *, clock: Clock | None = None) -> Container:
    """Construct the application from configuration.

    `settings` and `clock` are injectable so tests can build a real container
    against a throwaway database and a frozen clock, without touching the
    process environment.
    """
    settings = settings or Settings()

    configure_logging(level=settings.log_level, json_output=settings.use_json_logs)

    engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_max_overflow,
        connect_timeout_s=settings.db_connect_timeout_s,
    )
    return Container(
        settings=settings,
        engine=engine,
        session_factory=create_session_factory(engine),
        clock=clock or SystemClock(),
    )
