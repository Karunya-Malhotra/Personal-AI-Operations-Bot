"""Health endpoints, exercised without a database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.core.clock import FrozenClock
from app.core.environment import Environment
from app.main import create_app
from tests.conftest import make_container


class _BrokenEngine:
    """Stands in for an engine whose database is unreachable."""

    def connect(self) -> Any:
        raise ConnectionError("database is down")

    async def dispose(self) -> None:
        return None


@pytest.fixture
def app_with_broken_db(clean_env: None) -> Any:
    settings = Settings(app_env=Environment.DEV)
    container = make_container(
        settings=settings,
        engine=_BrokenEngine(),
        clock=FrozenClock(datetime(2026, 8, 14, tzinfo=UTC)),
    )
    app = create_app(container)

    # Replace the lifespan with a no-op. We are testing the *routes*, and the
    # real lifespan runs boot checks which correctly refuse an unreachable
    # database -- that behaviour has its own test. Note this is only possible
    # because create_app takes an injected container.
    @asynccontextmanager
    async def _no_lifespan(_app: Any) -> AsyncIterator[None]:
        yield

    app.router.lifespan_context = _no_lifespan
    return app


def test_liveness_ignores_dependencies(app_with_broken_db: Any) -> None:
    """Liveness must not check the database.

    A process that is alive but whose database blipped should not be killed:
    restarting it does not fix the database, and it does make the outage worse.
    """
    with TestClient(app_with_broken_db) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_degraded_when_database_is_down(app_with_broken_db: Any) -> None:
    with TestClient(app_with_broken_db) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"].startswith("error:")
