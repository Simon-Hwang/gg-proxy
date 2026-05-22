"""HITLCoordinator — pending-future router for HITL decisions.

A single coordinator serves an entire process; request(req_id) blocks until
resolve(req_id, decision) is called from elsewhere (REST endpoint, IM callback,
or test scaffold).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, cast


class HITLNotPending(LookupError):
    """resolve() called for a req_id not currently pending."""


@dataclass(frozen=True, slots=True)
class _PendingEntry:
    tool: str
    args: dict[str, Any]
    future: asyncio.Future[tuple[str, str | None]]


class HITLCoordinator:
    """Stores pending HITL requests by req_id; resolve() wakes the awaiter."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingEntry] = {}
        self._lock = asyncio.Lock()

    async def request(
        self,
        req_id: str,
        *,
        tool: str,
        args: dict[str, Any],
    ) -> Literal["accept", "deny"]:
        """Register req_id and block until resolve() is called."""
        async with self._lock:
            if req_id in self._pending:
                raise ValueError(f"req_id {req_id!r} already pending")
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[tuple[str, str | None]] = loop.create_future()
            self._pending[req_id] = _PendingEntry(tool=tool, args=args, future=fut)

        try:
            decision, _reason = await fut
        finally:
            async with self._lock:
                self._pending.pop(req_id, None)
        return cast(Literal["accept", "deny"], decision)

    async def resolve(
        self,
        req_id: str,
        decision: Literal["accept", "deny"],
        reason: str | None = None,
    ) -> None:
        """Wake the request(req_id) coroutine with decision."""
        async with self._lock:
            entry = self._pending.get(req_id)
            if entry is None or entry.future.done():
                raise HITLNotPending(req_id)
            entry.future.set_result((decision, reason))

    def pending_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a snapshot of all currently-pending requests.

        Returns a shallow-defensive-copy so mutating the returned structure
        cannot affect runner state. Safe to publish to dashboards / IM
        cards (which may inadvertently modify their input).

        Note: ``dict(e.args)`` is a shallow copy — sufficient because ``args``
        values are expected to be scalars per spec §6.2 (strings, ints, paths).
        If a caller stores nested dicts/lists in ``args``, they would still
        see mutations propagate one level deep; deep-copy is deferred until
        Plan 4 needs it.

        Must be called from the same event loop thread that owns this
        coordinator. Safe on CPython under single-threaded asyncio (dict
        comprehension is atomic at the bytecode level); not safe under
        threadpool/PyPy/multi-loop scenarios.
        """
        return {
            rid: {"tool": e.tool, "args": dict(e.args)}
            for rid, e in self._pending.items()
            if not e.future.done()
        }
