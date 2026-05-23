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
    accounting (Plan 6 D6.17). Reads the X-API-Key header verbatim;
    when absent, returns None (tests that disable auth never have one).
    The string is NOT validated here — the middleware did that before
    the request reached us.
    """
    key = request.headers.get("X-API-Key")
    return key if key else None


ManagerDep = Depends(get_manager)
StoreDep = Depends(get_store)
CoordinatorDep = Depends(get_coordinator)
ApiKeyIdDep = Depends(get_api_key_id)
