"""Plan 9 D9.12 — admin drain endpoint.

POST ``/api/v1/admin/drain`` — flips the pod into "no new traffic"
mode without touching its in-flight sessions. The K8s
``preStop`` hook uses this to give the load balancer time to
detach the pod from rotation before the SIGTERM lands; the
runbook explains the operator-driven scaling-down flow.

Two state mutations:

1. ``app.state.drained = True`` — the ``/readyz`` probe inspects
   this and returns 503 so K8s stops sending new traffic.
2. ``app.state.drain_started_at = datetime.now(UTC)`` — surfaces
   in the JSON response so operators can verify the timing of
   their preStop hook.

The endpoint is idempotent: a second drain returns the same
``drain_started_at`` as the first.

The endpoint does NOT abort in-flight sessions. That's the
``SessionManager.shutdown(grace_period_s)`` lifespan
responsibility — drain only signals "stop accepting new work";
graceful shutdown handles "wait for what's already running".
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from gg_relay.api.dependencies.require_role import require_role

logger = logging.getLogger("gg_relay.api.admin_drain")

router = APIRouter(prefix="/admin", tags=["admin-drain"])


@router.post("/drain")
async def drain(
    request: Request,
    _admin: Annotated[None, Depends(require_role("admin"))],
) -> dict[str, object]:
    """Signal "no new traffic" — ``/readyz`` returns 503 afterwards."""
    app = request.app
    if not getattr(app.state, "drained", False):
        app.state.drained = True
        app.state.drain_started_at = datetime.now(UTC)
        logger.info("admin_drain.activated")
        try:
            from gg_relay.tracing.metrics import DRAIN_REQUESTS_TOTAL

            DRAIN_REQUESTS_TOTAL.inc()
        except Exception:  # noqa: BLE001 — defensive
            pass
    else:
        logger.info("admin_drain.already_active")
    return {
        "drained": True,
        "drain_started_at": app.state.drain_started_at.isoformat(),
    }


@router.delete("/drain")
async def undrain(
    request: Request,
    _admin: Annotated[None, Depends(require_role("admin"))],
) -> dict[str, object]:
    """Cancel the drain — ``/readyz`` flips back to 200.

    Useful for operator-error recovery: an accidental drain
    POST during a deploy can be reverted without restarting the
    pod. Idempotent — second undrain is a no-op."""
    app = request.app
    was_drained = bool(getattr(app.state, "drained", False))
    if was_drained:
        app.state.drained = False
        app.state.drain_started_at = None
        logger.info("admin_drain.deactivated")
    return {"drained": False}
