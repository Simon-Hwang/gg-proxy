"""EventBus subscriber that drives :mod:`gg_relay.tracing.metrics`.

Independent from ``OtelSubscriber`` (which manages spans) so each can be
disabled separately. Reads typed RelayEvents and increments the
process-wide Prometheus registry.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Final

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
    SESSION_STATE_CHANGES,
    SESSIONS_ACTIVE,
    SESSIONS_BY_STATUS,
    SESSIONS_TOTAL,
    TOKENS_INPUT,
    TOKENS_OUTPUT,
)

logger = logging.getLogger("gg_relay.tracing.metrics_subscriber")

_RUNNING_STATE: Final[str] = "running"
_TERMINAL_STATES: Final[set[str]] = {"completed", "failed", "interrupted"}


class MetricsSubscriber:
    """Drains an EventBus and updates the Prometheus counters."""

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
            SESSION_STATE_CHANGES.labels(state=ev.to_state).inc()
            if ev.to_state == _RUNNING_STATE:
                SESSIONS_ACTIVE.inc()
            elif ev.to_state in _TERMINAL_STATES and ev.from_state == _RUNNING_STATE:
                SESSIONS_ACTIVE.dec()

    async def _completed(self, bus: EventBus) -> None:
        async for ev in bus.subscribe(SessionCompleted):
            assert isinstance(ev, SessionCompleted)  # noqa: S101
            SESSIONS_BY_STATUS.labels(status=ev.status).inc()
            tokens_in = int(ev.tokens.get("in", 0) or 0)
            tokens_out = int(ev.tokens.get("out", 0) or 0)
            if tokens_in:
                TOKENS_INPUT.inc(tokens_in)
            if tokens_out:
                TOKENS_OUTPUT.inc(tokens_out)
            if ev.cost_usd:
                COST_USD.inc(ev.cost_usd)

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
