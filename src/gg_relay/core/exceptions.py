"""Core-layer exceptions — Plan 7 Task 8 (D7.5).

Lives in :mod:`gg_relay.core` (zero external deps) so both the FastAPI
routers and the SessionManager can catch the same class without circular
imports through the store/session boundary.

:class:`HITLAlreadyResolved` is the user-facing companion to
:class:`gg_relay.store.exceptions.ConcurrencyError`: when two callers
race to ``POST /sessions/{sid}/hitl/{req_id}``, exactly one wins and
the other gets a ``409`` response whose body carries the winning
decision (so the loser sees what actually happened instead of just a
generic "already resolved" message).
"""
from __future__ import annotations

from typing import Any


class HITLAlreadyResolved(Exception):
    """HITL request was already resolved by an earlier decision.

    Carries the first decision (``status`` / ``resolver`` / ``reason``
    / ``resolved_at``) so the API layer can include it in the ``409``
    body. ``first_decision`` is optional because the in-memory race
    path (HITLNotPending → HITLAlreadyResolved) may not have a fresh
    DB row to read; callers should treat ``None`` as "we know the
    request was resolved but can't tell you who won".

    Plan 7 D7.5 / Task 8 — the partner of
    :class:`gg_relay.store.exceptions.ConcurrencyError` for the HITL
    workflow.
    """

    def __init__(
        self,
        req_id: str,
        *,
        first_decision: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"HITL request {req_id} already resolved")
        self.req_id = req_id
        self.first_decision = first_decision


__all__ = ["HITLAlreadyResolved"]
