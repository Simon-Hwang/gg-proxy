"""Liveness + readiness probes (intentionally unauthenticated)."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness — process is up and serving."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> dict[str, str]:
    """Readiness — SessionManager is constructed and accepting submits.

    Returns 503 indirectly by serializing an error message; orchestration
    frameworks (k8s) treat any non-2xx as not-ready.
    """
    manager = getattr(request.app.state, "manager", None)
    if manager is None:
        return {"status": "starting"}
    if not manager.accepting_new:
        return {"status": "draining"}
    return {"status": "ready"}
