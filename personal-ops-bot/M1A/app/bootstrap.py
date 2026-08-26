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
from app.core.llm import LLMProvider
from app.db.engine import create_engine, create_session_factory
from app.db.guard import assert_database_environment
from app.observability.logging import configure_logging, get_logger
from app.providers.llm import build_llm_provider

log = get_logger(__name__)


@dataclass(frozen=True)
class Container:
    """The assembled application. Passed down; never a global."""

    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock
    llm: LLMProvider

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
            llm_provider=self.llm.name,
            llm_model=self.settings.llm_model,
        )

    async def shutdown(self) -> None:
        await self.engine.dispose()


def build_container(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    llm: LLMProvider | None = None,
) -> Container:
    """Construct the application from configuration.

    `settings`, `clock` and `llm` are injectable so tests can build a real
    container against a throwaway database, a frozen clock and a scripted model,
    without touching the process environment or needing an API key.
    """
    settings = settings or Settings()

    configure_logging(level=settings.log_level, json_output=settings.use_json_logs)

    # The key is unwrapped here and nowhere else. Everything downstream receives
    # a constructed provider, never a credential and never Settings -- §19, and
    # the reason `ModelRequest` has no field that could carry one.
    api_key_by_provider = {
        "anthropic": settings.anthropic_api_key,
        "gemini": settings.gemini_api_key,
    }
    secret = api_key_by_provider.get(settings.llm_provider)

    engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_max_overflow,
        connect_timeout_s=settings.db_connect_timeout_s,
    )
    resolved_clock = clock or SystemClock()
    return Container(
        settings=settings,
        engine=engine,
        session_factory=create_session_factory(engine),
        clock=resolved_clock,
        llm=llm
        or build_llm_provider(
            provider=settings.llm_provider,
            clock=resolved_clock,
            api_key=secret.get_secret_value() if secret is not None else None,
            timeout_s=settings.llm_timeout_s,
        ),
    )
