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

Plan 7 D7.20 / Task 14: defence-in-depth pull-up. The coordinator now
accepts an optional ``store`` reference and, when supplied, consults
the row's ``status`` BEFORE flipping the in-process future. A row
whose status has already left ``pending`` (e.g. another worker
resolved it via direct ``upsert_hitl`` without going through this
coordinator) causes :class:`gg_relay.core.HITLAlreadyResolved` to be
raised directly — even if the in-process future is still pending.
This way, callers that catch :class:`HITLAlreadyResolved` get full
``first_decision`` payload without needing to know about the router-
layer escape hatch.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from gg_relay.core import HITLAlreadyResolved

if TYPE_CHECKING:
    from gg_relay.store.protocol import HITLStore


class HITLNotPending(LookupError):
    """``resolve()`` called for a ``req_id`` not currently pending.

    Plan 7 D7.20 / Task 14 — this remains a distinct exception class
    (not folded into :class:`HITLAlreadyResolved`) for backwards
    compatibility with Plan 4 tests + the router's existing
    ``except HITLNotPending`` block. New call sites that want full
    ``first_decision`` payloads should also catch
    :class:`gg_relay.core.HITLAlreadyResolved` which the coordinator
    raises whenever its optional ``store`` reports the row is no
    longer in ``pending`` state.
    """


@dataclass(frozen=True, slots=True)
class _PendingEntry:
    tool: str
    args: dict[str, Any]
    future: asyncio.Future[tuple[str, str | None]]
    session_id: str


def _first_decision_from_row(row: Any) -> dict[str, Any]:
    """Render a HITL DB row into the ``first_decision`` body fragment.

    Plan 7 D7.20 / Task 14 — mirrors the helper in
    :mod:`gg_relay.api.routers.hitl` so the coordinator can build the
    same shape without depending on the router module (which would
    create a circular import). ``resolved_at`` is converted to ISO
    8601 so callers can serialise the dict to JSON without further
    handling.
    """
    resolved_at = row.get("resolved_at")
    return {
        "status": row["status"],
        "resolver": row.get("resolver"),
        "reason": row.get("reason"),
        "resolved_at": (
            resolved_at.isoformat() if resolved_at is not None else None
        ),
    }


class HITLCoordinator:
    """Stores pending HITL requests by ``req_id``; resolve() wakes the awaiter.

    ``req_id`` is expected to follow the Plan 4 namespacing convention
    ``"{session_id}:{short_uuid}"``; the coordinator itself accepts any
    unique key. The optional ``session_id`` kwarg on
    :meth:`request` lets the caller annotate the entry for later filtering
    by :meth:`pending_snapshot` and :meth:`cancel_all`.

    Plan 7 D7.20 / Task 14 — the optional ``store`` kwarg wires in
    defence-in-depth race protection. When set, :meth:`resolve`
    consults the row's status BEFORE flipping the in-process future,
    so a row that was resolved out-of-band (e.g. another worker, or a
    direct ``upsert_hitl`` from a job) surfaces
    :class:`gg_relay.core.HITLAlreadyResolved` immediately with a
    fully-populated ``first_decision`` body. Tests that don't care
    about cross-worker races leave ``store=None``.
    """

    def __init__(self, *, store: HITLStore | None = None) -> None:
        self._pending: dict[str, _PendingEntry] = {}
        self._lock = asyncio.Lock()
        self._store = store

    @property
    def store(self) -> HITLStore | None:
        return self._store

    def attach_store(self, store: HITLStore) -> None:
        """Late-bind the optional DB store.

        Used by :mod:`gg_relay.api.main` so the lifespan can construct
        the coordinator before the engine and then wire the store
        once the engine is initialised. Idempotent — overwriting an
        existing reference is allowed (tests may rebind to a fresh
        DB between fixture phases).
        """
        self._store = store

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

        Plan 7 D7.20 / Task 14 — when the coordinator was constructed
        with an optional ``store`` reference, we consult the DB row's
        status BEFORE flipping the future:

        * row absent or in ``pending`` → proceed as before (in-process
          fast path).
        * row in any non-pending status → raise
          :class:`gg_relay.core.HITLAlreadyResolved` directly with a
          fully-populated ``first_decision`` from the row.

        This covers the cross-worker race the router cannot see (one
        worker resolves via :meth:`HITLStore.upsert_hitl` directly,
        the other worker's coordinator still holds the pending
        future). Callers that only have the in-memory coordinator
        will continue to see :class:`HITLNotPending` from the
        existing fast-path branch.
        """
        if self._store is not None:
            cur = await self._store.get_hitl(req_id)
            if cur is not None and cur["status"] != "pending":
                raise HITLAlreadyResolved(
                    req_id,
                    first_decision=_first_decision_from_row(cur),
                )
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
