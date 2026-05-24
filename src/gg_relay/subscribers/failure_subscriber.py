"""Subscribe to terminal session transitions for alert routing (Plan 8 D8.7).

The subscriber consumes typed :class:`SessionStateChanged` events off
the bus, filters to **terminal** transitions
(``to_state ∈ {failed, cancelled, completed}``), enriches each event
with the session's ``owner`` + ``tags`` (looked up from the store on
demand because the typed event carries neither), and forwards the
result to :class:`AlertRouter` for rule matching and IM dispatch.

The spec for D8.7 names three separate event classes
(``SessionFailed`` / ``SessionCancelled`` / ``SessionCompleted``); the
codebase models all terminal transitions as a single typed event
(:class:`SessionStateChanged` with a discriminated ``to_state`` field)
plus the wire-level :class:`SessionCompleted` (frame-derived). We
subscribe to :class:`SessionStateChanged` because it is the **canonical
manager-side signal** — it ALWAYS fires from
``SessionManager._run.finally`` (even when the runner crashes before
emitting a ``session.end`` wire frame) and it carries the canonical
``end_reason`` in its ``reason`` field, which is the input the
:class:`AlertRouter` rules key off.

User-initiated cancellations (``end_reason in {"user_cancel",
"user_request"}``) are filtered out **at the subscriber level** —
before the alert router sees them — because they're never operational
incidents: the operator is already aware of the action they just
took. Operationally-driven cancellations (``"timeout"``,
``"timeout_recovered"``, ``"paused_timeout"``, etc.) still flow
through, and the router's ``cancel`` rule list filters further.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from gg_relay.core import EventBus, SessionStateChanged
from gg_relay.subscribers.alert_router import AlertRouter

logger = logging.getLogger("gg_relay.subscribers.failure_subscriber")


_TERMINAL_TO_EVENT_TYPE: dict[str, str] = {
    "failed": "session_failed",
    "cancelled": "session_cancelled",
    "completed": "session_completed",
}
"""Map :attr:`SessionStateChanged.to_state` to the
:class:`AlertRouter` event_type vocabulary. ``"interrupted"`` is
deliberately absent — it's emitted by the startup recovery path and
isn't a fresh operational signal worth alerting on (the original
incident already fired)."""


_USER_INITIATED_CANCEL_REASONS: frozenset[str] = frozenset(
    {"user_cancel", "user_request"}
)
"""Cancel ``end_reason`` values that come from a human operator's
action (``SessionManager.cancel(reason="user_request")`` is the
canonical path; ``"user_cancel"`` is reserved for a future
operator-friendly synonym). Never alert on these — the human is
already aware."""


class FailureSubscriber:
    """Bus → :class:`AlertRouter` filter for terminal session events.

    Constructed cheaply; call :meth:`start` to spawn the long-running
    consumer task and :meth:`stop` from the lifespan ``finally`` to
    drain it. The class also exposes :meth:`handle` as a public,
    awaitable entry point so unit tests can drive single events
    without spinning a bus + task pair.

    ``store`` is optional — when set, the subscriber calls
    ``store.get_session(sid)`` to fetch ``owner`` + ``tags`` for
    rule matching. When unset (typical for unit tests that construct
    events with these fields attached directly), :meth:`handle`
    falls back to ``getattr(event, "owner", None)`` /
    ``getattr(event, "tags", [])`` so tests can attach the metadata
    inline.
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        alert_router: AlertRouter,
        store: Any = None,
    ) -> None:
        self._bus = bus
        self._router = alert_router
        self._store = store
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    async def handle(self, event: SessionStateChanged) -> bool:
        """Inspect ONE terminal :class:`SessionStateChanged` and
        forward it to the router. Returns the router's verdict
        (``True`` iff a card was dispatched).

        Non-terminal transitions (e.g. ``queued → running``,
        ``running → paused``) return ``False`` immediately; the
        consumer loop swallows the return value, so this is purely
        for the test surface.
        """
        event_type = _TERMINAL_TO_EVENT_TYPE.get(event.to_state)
        if event_type is None:
            return False
        end_reason = event.reason or "unknown"
        if (
            event_type == "session_cancelled"
            and end_reason in _USER_INITIATED_CANCEL_REASONS
        ):
            return False
        owner, tags = await self._resolve_meta(event)
        return await self._router.dispatch(
            event_type=event_type,
            session_id=event.session_id,
            owner=owner,
            tags=tags,
            end_reason=end_reason,
            event=event,
        )

    async def _resolve_meta(
        self, event: SessionStateChanged
    ) -> tuple[str | None, list[str]]:
        """Best-effort lookup of ``(owner, tags)``.

        Resolution order:
          1. Attributes already on the event (``hasattr`` check —
             used by unit tests that pass a duck-typed event with
             owner/tags inlined; production :class:`SessionStateChanged`
             is ``slots=True`` and has neither attribute so this branch
             is skipped automatically)
          2. ``store.get_session(session_id)`` — production path
          3. ``(None, [])`` — caller treats as anonymous + untagged
        """
        if hasattr(event, "owner") or hasattr(event, "tags"):
            owner_attr = getattr(event, "owner", None)
            tags_attr = getattr(event, "tags", None)
            return owner_attr, list(tags_attr or [])
        if self._store is None:
            return None, []
        try:
            row = await self._store.get_session(event.session_id)
        except Exception:
            logger.debug(
                "store.get_session raised for %s; alerting anonymously",
                event.session_id,
                exc_info=True,
            )
            return None, []
        if row is None:
            return None, []
        owner = row.get("owner") if hasattr(row, "get") else None
        tags_raw = row.get("tags") if hasattr(row, "get") else None
        tags: list[str] = (
            [str(t) for t in tags_raw]
            if isinstance(tags_raw, list | tuple)
            else []
        )
        return owner, tags

    def start(self) -> asyncio.Task[None]:
        """Spawn the consumer task. Idempotent; subsequent calls
        return the existing task so the lifespan can store the
        handle without worrying about double-start races.
        """
        if self._task is not None:
            return self._task
        self._task = asyncio.create_task(
            self._run(), name="failure-subscriber"
        )
        return self._task

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _run(self) -> None:
        iterator = self._bus.subscribe(SessionStateChanged)
        try:
            async for event in iterator:
                if not isinstance(event, SessionStateChanged):
                    continue
                try:
                    await self.handle(event)
                except Exception:
                    logger.exception(
                        "FailureSubscriber.handle raised session_id=%s",
                        event.session_id,
                    )
        except asyncio.CancelledError:
            raise
