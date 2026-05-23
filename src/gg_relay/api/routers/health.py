"""Liveness + readiness probes (intentionally unauthenticated).

Plan 7 Task 15 (D7.22): ``/readyz`` is upgraded to a real readiness check
(SELECT 1 on the configured engine + manager-not-draining gate). It now
returns HTTP 503 when either gate fails so k8s / load balancers can pull
the pod out of rotation. ``/healthz`` stays a pure liveness probe (process
is up + event loop ticking) and intentionally does NOT touch the DB —
a brief DB outage must not restart the relay process.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness — process is up and serving."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> dict[str, str]:
    """Readiness — SessionManager is accepting submits AND the DB is reachable.

    Returns 503 with a ``detail`` string identifying the failing gate:

      * ``"starting"``         — lifespan hasn't finished wiring the manager
      * ``"manager_draining"`` — :meth:`SessionManager.shutdown` ran
      * ``"db_unreachable: <ExceptionType>"`` — ``SELECT 1`` raised
    """
    manager = getattr(request.app.state, "manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="starting")
    # ``accepting_new=False`` is the public read of the manager's draining
    # flag (set by shutdown()); the detail string matches the plan contract
    # so k8s probes can grep for a stable token.
    if not manager.accepting_new:
        raise HTTPException(status_code=503, detail="manager_draining")
    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as e:  # noqa: BLE001 — engine drivers raise many types
            raise HTTPException(
                status_code=503,
                detail=f"db_unreachable: {type(e).__name__}",
            ) from e
    return {"status": "ready"}
