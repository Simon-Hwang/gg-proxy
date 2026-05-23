"""EventBus subscriber that drives :mod:`gg_relay.tracing.metrics`.

Independent from ``OtelSubscriber`` (which manages spans) so each can be
disabled separately. Reads typed RelayEvents and increments the
process-wide Prometheus registry.

Plan 7 Task 15 (D7.21) additions:

  * ``SESSION_DURATION`` histogram is now observed at the
    RUNNING → terminal transition (start times tracked in
    :attr:`_start_times`).
  * Token / cost ingestion goes through :meth:`_on_aggregates`, which
    accepts canonical attribute names (``input_tokens`` /
    ``output_tokens`` / ``cost_usd``) with backward-compat fallbacks
    for the legacy :class:`SessionCompleted.tokens` dict shape
    (``in`` / ``out``). Callers building a future SessionAggregates
    event type can publish it directly without changing the subscriber.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Final

from gg_relay.core import (
    EventBus,
    HITLRequested,
    HITLResolved,
    InstallError,
    SessionCompleted,
    SessionCreated,
    SessionStateChanged,
)
from gg_relay.tracing.metrics import (
    COST_USD,
    ERRORS,
    HITL_REQUESTS,
    HITL_RESOLVED,
    SESSION_DURATION,
    SESSION_STATE_CHANGES,
    SESSIONS_ACTIVE,
    SESSIONS_BY_STATUS,
    SESSIONS_TOTAL,
    TOKENS_INPUT,
    TOKENS_OUTPUT,
)

logger = logging.getLogger("gg_relay.tracing.metrics_subscriber")

_RUNNING_STATE: Final[str] = "running"
_TERMINAL_STATES: Final[set[str]] = {
    "completed",
    "failed",
    "interrupted",
    "cancelled",
}


class MetricsSubscriber:
    """Drains an EventBus and updates the Prometheus counters."""

    def __init__(self) -> None:
        # Per-session RUNNING start times for SESSION_DURATION observation.
        # Cleared at the terminal transition; if a session is interrupted
        # before publishing a terminal state the entry is leaked, which is
        # bounded by the SessionManager's own state-machine guarantees.
        self._start_times: dict[str, float] = {}

    async def run(self, bus: EventBus) -> None:
        tasks = [
            asyncio.create_task(self._created(bus), name="metrics.created"),
            asyncio.create_task(self._state(bus), name="metrics.state"),
            asyncio.create_task(self._completed(bus), name="metrics.completed"),
            asyncio.create_task(self._hitl_req(bus), name="metrics.hitl_req"),
            asyncio.create_task(self._hitl_res(bus), name="metrics.hitl_res"),
            asyncio.create_task(self._errors(bus), name="metrics.errors"),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise

    async def _created(self, bus: EventBus) -> None:
        async for _ in bus.subscribe(SessionCreated):
            SESSIONS_TOTAL.inc()

    async def _state(self, bus: EventBus) -> None:
        async for ev in bus.subscribe(SessionStateChanged):
            assert isinstance(ev, SessionStateChanged)  # noqa: S101
            self._on_state(ev)

    def _on_state(self, ev: SessionStateChanged) -> None:
        SESSION_STATE_CHANGES.labels(state=ev.to_state).inc()
        sid = ev.session_id
        if ev.to_state == _RUNNING_STATE:
            SESSIONS_ACTIVE.inc()
            # First RUNNING transition starts the duration clock. Resume
            # transitions (PAUSED → RUNNING) preserve the original start
            # time so SESSION_DURATION measures wall-clock lifetime, not
            # just the final run segment.
            if sid and sid not in self._start_times:
                self._start_times[sid] = time.monotonic()
        elif ev.to_state in _TERMINAL_STATES:
            if ev.from_state == _RUNNING_STATE:
                SESSIONS_ACTIVE.dec()
            if sid:
                started = self._start_times.pop(sid, None)
                if started is not None:
                    SESSION_DURATION.observe(time.monotonic() - started)

    async def _completed(self, bus: EventBus) -> None:
        async for ev in bus.subscribe(SessionCompleted):
            assert isinstance(ev, SessionCompleted)  # noqa: S101
            SESSIONS_BY_STATUS.labels(status=ev.status).inc()
            self._on_aggregates(ev)

    def _on_aggregates(self, event: Any) -> None:
        """Update token / cost counters from a session-aggregate event.

        Field-name resolution (canonical first, so callers migrating to
        a future ``SessionAggregates`` event type win immediately):

          1. ``event.input_tokens`` / ``event.output_tokens``  (canonical)
          2. ``event.input`` / ``event.output``                (alt name)
          3. ``event.in_`` / ``event.out``                     (Python alt)
          4. ``event.tokens["in"]`` / ``event.tokens["out"]``  (legacy
             :class:`SessionCompleted.tokens` dict shape)

        Cost is read from ``event.cost_usd`` (only supported attribute).
        Falsy / missing values are skipped so partial events don't poison
        the totals.
        """
        in_toks = _first_present(
            event,
            "input_tokens",
            "input",
            "in_",
        )
        out_toks = _first_present(
            event,
            "output_tokens",
            "output",
            "out",
        )
        # Legacy SessionCompleted.tokens dict fallback (only consulted when
        # the attribute-style lookup didn't find anything).
        tokens_dict = getattr(event, "tokens", None)
        if in_toks in (None, 0) and isinstance(tokens_dict, dict):
            in_toks = tokens_dict.get("in") or tokens_dict.get("input_tokens")
        if out_toks in (None, 0) and isinstance(tokens_dict, dict):
            out_toks = tokens_dict.get("out") or tokens_dict.get("output_tokens")
        cost = getattr(event, "cost_usd", None) or 0.0

        in_toks_int = _safe_int(in_toks)
        out_toks_int = _safe_int(out_toks)
        if in_toks_int:
            TOKENS_INPUT.inc(in_toks_int)
        if out_toks_int:
            TOKENS_OUTPUT.inc(out_toks_int)
        if cost:
            COST_USD.inc(float(cost))

    async def _hitl_req(self, bus: EventBus) -> None:
        async for _ in bus.subscribe(HITLRequested):
            HITL_REQUESTS.inc()

    async def _hitl_res(self, bus: EventBus) -> None:
        async for ev in bus.subscribe(HITLResolved):
            assert isinstance(ev, HITLResolved)  # noqa: S101
            HITL_RESOLVED.labels(decision=ev.decision).inc()

    async def _errors(self, bus: EventBus) -> None:
        async for ev in bus.subscribe(InstallError):
            assert isinstance(ev, InstallError)  # noqa: S101
            ERRORS.labels(kind=ev.code).inc()


def _first_present(obj: Any, *names: str) -> Any:
    """Return the first attribute value among ``names`` that is set + truthy.

    Falls through ``None`` and ``0`` so the legacy ``tokens`` dict
    fallback in :meth:`MetricsSubscriber._on_aggregates` still gets a
    chance to fill the value.
    """
    for n in names:
        v = getattr(obj, n, None)
        if v:
            return v
    return None


def _safe_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
