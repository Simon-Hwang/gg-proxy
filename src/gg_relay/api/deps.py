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


ManagerDep = Depends(get_manager)
StoreDep = Depends(get_store)
CoordinatorDep = Depends(get_coordinator)
