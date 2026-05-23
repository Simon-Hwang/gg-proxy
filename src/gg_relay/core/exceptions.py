"""Core-layer exceptions — Plan 7 Task 8 (D7.5) + Task 13 (D7.17).

Lives in :mod:`gg_relay.core` (zero external deps) so both the FastAPI
routers and the SessionManager can catch the same class without circular
imports through the store/session boundary.

:class:`HITLAlreadyResolved` is the user-facing companion to
:class:`gg_relay.store.exceptions.ConcurrencyError`: when two callers
race to ``POST /sessions/{sid}/hitl/{req_id}``, exactly one wins and
the other gets a ``409`` response whose body carries the winning
decision (so the loser sees what actually happened instead of just a
generic "already resolved" message).

:class:`DurableEventDropError` is raised by the EventBus when a durable
tier event cannot be persisted (no store configured in strict mode, or
the configured store's :meth:`persist` raised). Callers MUST handle it
— silently dropping a durable event would defeat the entire purpose of
the disk-backed bus (Plan 7 D7.17).
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


class DurableEventDropError(Exception):
    """Raised when a durable-tier event cannot reach its persistent store.

    Two trigger conditions on :meth:`gg_relay.core.event_bus.EventBus.publish`:

    1. ``durable_store`` is unset AND the bus was constructed with
       ``strict_durable=True`` — publishing a ``delivery_tier="durable"``
       event without a backing store would silently drop audit data, so
       the bus raises instead of fanning out.
    2. The configured store's ``persist`` raised — the bus wraps the
       underlying exception so callers can ``except DurableEventDropError``
       without coupling to SQLAlchemy / Redis exception hierarchies.

    Callers (SessionManager, IM publish, SSE) MUST handle this — they
    may retry, surface to the operator, or trigger graceful degradation,
    but they must NOT swallow it. Plan 7 Task 15 will add a Prometheus
    counter for raised drops so operators can alert on the rate.
    """


__all__ = ["DurableEventDropError", "HITLAlreadyResolved"]
