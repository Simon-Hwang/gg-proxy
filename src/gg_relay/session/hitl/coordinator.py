"""HITLCoordinator — pending-future router for HITL decisions.

A single coordinator serves an entire process; ``request(req_id)`` blocks
until ``resolve(req_id, decision)`` is called from elsewhere (REST endpoint,
IM callback, or test scaffold).

Plan 4 D4 additions:
  - :meth:`cancel_all` — graceful shutdown helper; resolves every pending
    request as ``deny`` so awaiters unblock and the SessionManager can
    drain cleanly.
  - ``session_id`` tracking on each pending entry so list/snapshot calls
    can filter by session, and so :meth:`cancel_all` can support a
    ``session_id`` predicate when cancelling per-session.
  - ``reason`` is now plumbed through resolve and returned alongside the
    decision via :meth:`request_with_reason` for callers that want to
    persist the rationale.

Plan 7 D7.5 / Task 8: race-condition coverage. The coordinator itself
remains the in-process source of truth (so two concurrent ``resolve``
calls in the same worker see the second one raise
:class:`HITLNotPending` once the first has fired the future). The DB
version-check defence-in-depth lives one level up in
:mod:`gg_relay.api.routers.hitl`, which:

* reads the row's ``version`` before issuing
  :meth:`HITLStore.upsert_hitl(expected_version=...)`,
* catches :class:`HITLNotPending` from the coordinator and converts it
  to :class:`gg_relay.core.HITLAlreadyResolved` with the winning
  decision pulled from the DB (so the loser's 409 body shows what
  actually won), and
* maps :class:`gg_relay.store.exceptions.ConcurrencyError` from the
  upsert to the same :class:`HITLAlreadyResolved` shape (covers
  multi-process deployments where two API workers race past their
  separate in-memory coordinators).

The HITL workflow deliberately does **not** retry on version mismatch
— a contested resolve has at most one winner and surfaces a 409
immediately rather than swallowing the conflict.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, cast


class HITLNotPending(LookupError):
    """``resolve()`` called for a ``req_id`` not currently pending."""


@dataclass(frozen=True, slots=True)
class _PendingEntry:
    tool: str
    args: dict[str, Any]
    future: asyncio.Future[tuple[str, str | None]]
    session_id: str


class HITLCoordinator:
    """Stores pending HITL requests by ``req_id``; resolve() wakes the awaiter.

    ``req_id`` is expected to follow the Plan 4 namespacing convention
    ``"{session_id}:{short_uuid}"``; the coordinator itself accepts any
    unique key. The optional ``session_id`` kwarg on
    :meth:`request` lets the caller annotate the entry for later filtering
    by :meth:`pending_snapshot` and :meth:`cancel_all`.
    """

    def __init__(self) -> None:
        self._pending: dict[str, _PendingEntry] = {}
        self._lock = asyncio.Lock()

    async def request(
        self,
        req_id: str,
        *,
        tool: str,
        args: dict[str, Any],
        session_id: str = "",
    ) -> Literal["accept", "deny"]:
        """Register ``req_id`` and block until ``resolve()`` is called.

        Returns the decision as a literal. To recover the optional reason
        as well, use :meth:`request_with_reason`.
        """
        decision, _ = await self.request_with_reason(
            req_id, tool=tool, args=args, session_id=session_id
        )
        return decision

    async def request_with_reason(
        self,
        req_id: str,
        *,
        tool: str,
        args: dict[str, Any],
        session_id: str = "",
    ) -> tuple[Literal["accept", "deny"], str | None]:
        """Same as :meth:`request` but returns ``(decision, reason)``."""
        async with self._lock:
            if req_id in self._pending:
                raise ValueError(f"req_id {req_id!r} already pending")
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[tuple[str, str | None]] = loop.create_future()
            self._pending[req_id] = _PendingEntry(
                tool=tool, args=args, future=fut, session_id=session_id
            )

        try:
            decision, reason = await fut
        finally:
            async with self._lock:
                self._pending.pop(req_id, None)
        return cast(Literal["accept", "deny"], decision), reason

    async def resolve(
        self,
        req_id: str,
        decision: Literal["accept", "deny"],
        reason: str | None = None,
    ) -> None:
        """Wake the ``request(req_id)`` coroutine with ``decision`` + ``reason``.

        Raises :class:`HITLNotPending` if the request was never registered
        or has already been resolved.
        """
        async with self._lock:
            entry = self._pending.get(req_id)
            if entry is None or entry.future.done():
                raise HITLNotPending(req_id)
            entry.future.set_result((decision, reason))

    async def cancel_all(
        self, *, reason: str = "shutdown", session_id: str | None = None
    ) -> int:
        """Resolve every pending request (optionally scoped to ``session_id``)
        with ``deny`` + ``reason``. Returns the count of requests cancelled.

        Used by :meth:`SessionManager.shutdown` to unblock every awaiter so
        the bridge / runner can terminate cleanly. Idempotent — calling
        twice returns 0 the second time.
        """
        count = 0
        async with self._lock:
            for entry in self._pending.values():
                if entry.future.done():
                    continue
                if session_id is not None and entry.session_id != session_id:
                    continue
                entry.future.set_result(("deny", reason))
                count += 1
        return count

    def pending_snapshot(
        self, *, session_id: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """Return a snapshot of currently-pending requests.

        Optional ``session_id`` filter narrows results to one session — used
        by the dashboard's per-session HITL view and by
        ``SessionManager.cancel`` when cancelling a single session's pending
        requests.

        Returns a shallow-defensive-copy so mutating the returned structure
        cannot affect runner state. Safe to publish to dashboards / IM
        cards (which may inadvertently modify their input).
        """
        return {
            rid: {
                "tool": e.tool,
                "args": dict(e.args),
                "session_id": e.session_id,
            }
            for rid, e in self._pending.items()
            if not e.future.done()
            and (session_id is None or e.session_id == session_id)
        }
