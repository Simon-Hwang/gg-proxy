"""FastAPI dependencies.

The app stores shared services on ``app.state``; these helpers expose them
to handlers via ``Depends`` so request signatures stay typed.
"""
from __future__ import annotations

from fastapi import Depends, Request

from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.manager import SessionManager
from gg_relay.store import SessionRepository


def get_manager(request: Request) -> SessionManager:
    return request.app.state.manager  # type: ignore[no-any-return]


def get_store(request: Request) -> SessionRepository:
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
