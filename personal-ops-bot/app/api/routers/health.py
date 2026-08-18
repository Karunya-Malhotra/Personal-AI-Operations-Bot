"""Health endpoints.

Two endpoints, not one, and the distinction is operational rather than cosmetic.

`/health` is **liveness**: is this process running and able to respond? It
touches nothing. It must never check the database, because a process that is
alive but whose database is briefly unreachable should not be killed and
restarted -- restarting it does not fix the database and does make the outage
worse.

`/health/ready` is **readiness**: should traffic be sent here? It checks
dependencies and is allowed to fail. From M2 it also reports queue and storage
reachability.

They exist in M1A, before there is any traffic, because `docker compose` needs
a healthcheck to sequence startup, and because the shape of "what does healthy
mean" should be decided once rather than argued about during an incident.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness: process is up. Checks no dependencies.")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness: dependencies are reachable.")
async def ready(request: Request, response: Response) -> dict[str, Any]:
    container = request.app.state.container
    checks: dict[str, str] = {}

    try:
        async with container.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"

    ok = all(v == "ok" for v in checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ok else "degraded", "checks": checks}
