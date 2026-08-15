"""FastAPI application factory (the `api` entrypoint).

`create_app()` is a function rather than a module-level `app = FastAPI()` for
the same reason `Settings` is not a module global: a module-level app is
constructed at import time, which means importing it in a test runs the real
bootstrap against the real environment.

The lifespan handler owns the container's life. Boot checks run there, so a
failed check prevents the server from binding a port at all -- uvicorn exits
non-zero and a supervisor sees a clean failure, rather than a process that is
up and serving errors.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routers import health
from app.bootstrap import Container, build_container
from app.observability.logging import get_logger

log = get_logger(__name__)


def create_app(container: Container | None = None) -> FastAPI:
    built = container or build_container()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await built.startup()
        log.info("api.started", env=built.settings.app_env.value)
        try:
            yield
        finally:
            await built.shutdown()
            log.info("api.stopped")

    app = FastAPI(
        title="Personal Ops Bot",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if not built.settings.is_prod else None,
    )
    app.state.container = built
    app.include_router(health.router)
    return app
