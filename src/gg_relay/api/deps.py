"""FastAPI dependencies.

The app stores shared services on ``app.state``; these helpers expose them
to handlers via ``Depends`` so request signatures stay typed.

Plan 7 Task 5 (D7.4): ``get_store`` is typed against the
:class:`SessionStore` Protocol rather than the concrete
:class:`SqlAlchemyStore`. The runtime instance is still
:class:`SqlAlchemyStore` (assigned during app startup); routers that
need frame/HITL operations should depend on this same store object —
the concrete class implements all three Protocols.
"""
from __future__ import annotations

from fastapi import Depends, Request

from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.manager import SessionManager
from gg_relay.store import SessionStore


def get_manager(request: Request) -> SessionManager:
    return request.app.state.manager  # type: ignore[no-any-return]


def get_store(request: Request) -> SessionStore:
    return request.app.state.store  # type: ignore[no-any-return]


def get_coordinator(request: Request) -> HITLCoordinator:
    return request.app.state.coordinator  # type: ignore[no-any-return]


def get_api_key_id(request: Request) -> str | None:
    """Per-request opaque identifier used by SessionManager's pause
    accounting (Plan 6 D6.17 + Plan 7 D7.15).

    Returns ``request.state.api_key_id`` — the 16-char sha256 prefix
    that :class:`APIKeyAuthMiddleware` populates on successful auth.
    The plaintext key never leaves the middleware; downstream code
    (rate limiter, pause accounting, audit logs) gets a stable hash
    that's safe to log, persist, and emit in metrics. When the
    middleware was bypassed (``allow_no_keys=True`` test paths, exempt
    webhook routes) the attribute is absent and we return ``None``.
    """
    return getattr(request.state, "api_key_id", None) or None


ManagerDep = Depends(get_manager)
StoreDep = Depends(get_store)
CoordinatorDep = Depends(get_coordinator)
ApiKeyIdDep = Depends(get_api_key_id)
