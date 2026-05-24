"""Cost metric subscriber stub — Plan 8 D8.30 / Task 23.

Intentionally minimal: per-session cost / count counters already live
in :mod:`gg_relay.tracing.metrics_subscriber` (Plan 7 D7.15 — see
``MetricsSubscriber._on_aggregates`` which calls ``COST_USD.inc``
and ``SESSIONS_BY_STATUS.labels(...)`` on every ``SessionCompleted``).
Re-implementing the same counters here would double-count.

This module instead reserves the integration point for the
**per-owner labels** extension that Plan 8 Task 20 / D8.13 will land
once the Grafana panels are designed (a label cardinality decision —
one label per owner is fine at single-team scale, but the Prometheus
guidance against high-cardinality labels means we want explicit
sign-off before rolling it into the global registry).

The class is constructed by the lifespan IF / WHEN the wiring lands:
``CostMetricSubscriber(cost_counter=..., count_counter=...)``. Until
Task 20 the lifespan never instantiates it, so import-time side
effects are zero. The :meth:`register` hook is the seam where the
event bus subscription will go — kept signature-stable so the future
patch is mechanical.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("gg_relay.subscribers.cost_metric_subscriber")


class CostMetricSubscriber:
    """Bump per-owner Prometheus counters at session terminal transitions.

    Plan 8 D8.30 / Task 23. Two counters are expected (both optional
    so the subscriber composes cleanly with a partial registry):

      * ``cost_counter`` — ``gg_relay_session_cost_usd_total{owner,
        status}`` Counter. Incremented by ``event.total_cost_usd`` on
        every terminal transition.
      * ``count_counter`` — ``gg_relay_session_count_total{owner,
        status}`` Counter. Incremented by 1 on every terminal
        transition (regardless of cost).

    Both counters are passed in so the registry remains owned by
    :mod:`gg_relay.tracing.metrics` — this class is a pure
    transformer between EventBus payloads and the counter API.

    Wiring (deferred to Task 20): the lifespan will construct one
    instance, subscribe to :class:`SessionStateChanged`, and call
    :meth:`on_session_end` when ``to_state`` is one of the terminal
    states (``completed`` / ``failed`` / ``interrupted`` /
    ``cancelled``). Until then this module is import-only.
    """

    def __init__(
        self,
        *,
        cost_counter: Any = None,
        count_counter: Any = None,
    ) -> None:
        self._cost_counter = cost_counter
        self._count_counter = count_counter

    async def on_session_end(self, event: Any) -> None:
        """Update counters when a session reaches a terminal state.

        Defensive: missing attributes / counter errors are logged and
        swallowed — a Prometheus mishap must never crash the bus
        consumer. ``owner`` falls back to ``"unknown"`` so the label
        cardinality stays bounded (otherwise a misconfigured legacy
        session would emit a ``None`` label which Prometheus rejects).
        """
        try:
            owner = getattr(event, "owner", None) or "unknown"
            status = getattr(event, "to_state", None) or "unknown"
            cost = float(getattr(event, "total_cost_usd", 0.0) or 0.0)
            if self._count_counter is not None:
                self._count_counter.labels(owner=owner, status=status).inc()
            if self._cost_counter is not None and cost:
                self._cost_counter.labels(
                    owner=owner, status=status
                ).inc(cost)
        except Exception:  # pragma: no cover — defensive
            logger.warning(
                "cost_metric_subscriber on_session_end failed", exc_info=True
            )

    def register(self, event_bus: Any) -> None:
        """Reserved hook for the Task 20 / D8.13 wiring.

        Deliberately a no-op until the Grafana panel design pins
        the label cardinality budget. The signature is the seam
        the future patch will fill — typically a
        ``bus.subscribe(SessionStateChanged)`` async task that
        filters on ``to_state in TERMINAL_STATES`` and forwards to
        :meth:`on_session_end`.
        """
        del event_bus


__all__ = ["CostMetricSubscriber"]
