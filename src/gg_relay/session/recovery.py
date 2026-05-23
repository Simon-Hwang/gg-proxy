"""Startup recovery — Plan 4 D4.6 + Plan 7 D7.18 (Task 14).

When the relay restarts, two classes of session need attention:

* ``running`` rows (Plan 4 D4.6) — the previous process crashed
  mid-flight. We take the conservative stance: mark them
  ``interrupted`` and let a human resubmit.  See
  :func:`recover_on_startup`.
* ``paused`` rows (Plan 7 D7.18 / Task 14) — the in-process
  paused-timeout watchdog was lost when the previous process exited.
  Without intervention, paused sessions stay paused forever (the
  watchdog was per-task asyncio sleep, not persisted). The recovery
  hook walks each paused row and either re-arms the watchdog with the
  remaining window or cancels the row if the elapsed time already
  exceeded ``paused_timeout_s``. See :func:`recover_paused_timers`.

Both helpers are idempotent and safe to invoke on every lifespan start
(they no-op once the corresponding rows have been processed).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from gg_relay.store import SessionRepository

logger = logging.getLogger("gg_relay.session.recovery")


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Outcome of :func:`recover_on_startup`.

    ``interrupted_ids`` is the canonical list of sessions that were rolled
    forward from ``running`` to ``interrupted``; the dashboard / lifespan
    log surfaces ``interrupted_count`` to operators.
    """

    interrupted_count: int
    interrupted_ids: tuple[str, ...]


async def recover_on_startup(store: SessionRepository) -> RecoveryReport:
    """Promote every in-flight ``running`` row to ``interrupted``.

    Returns the count and ids touched. Idempotent — calling twice after the
    first pass returns ``RecoveryReport(0, ())``.
    """
    ids = await store.mark_in_flight_as_interrupted()
    return RecoveryReport(interrupted_count=len(ids), interrupted_ids=tuple(ids))


class _PausedStoreLike(Protocol):
    """Minimal store surface needed by :func:`recover_paused_timers`.

    Defined locally so tests can pass a lightweight fake without
    inheriting from the full :class:`SessionStore` Protocol.
    """

    async def list_paused(self) -> Any: ...


class _PausedManagerLike(Protocol):
    """Minimal manager surface needed by :func:`recover_paused_timers`.

    ``_arm_paused_timer`` is the in-class private API; we use a
    Protocol here so unit tests can pass a fake manager exposing
    just the two methods we touch.
    """

    async def cancel(self, sid: str, *, reason: str = ...) -> None: ...

    def _arm_paused_timer(
        self, sid: str, *, remaining_s: float | None = ...
    ) -> None: ...


async def recover_paused_timers(
    manager: _PausedManagerLike,
    store: _PausedStoreLike,
    *,
    paused_timeout_s: float,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Re-arm or cancel paused-session watchdogs on startup (Plan 7 D7.18).

    Walk every ``paused`` row whose ``paused_at`` is set:

    * ``elapsed > paused_timeout_s`` — the watchdog already fired in
      the previous process (or would have, if the process hadn't
      crashed). Cancel the session with
      ``reason='paused_timeout_recovered'`` so the row settles
      deterministically.
    * Otherwise — re-arm the asyncio timer with the remaining window
      (``paused_timeout_s - elapsed``) so the new process honours the
      original deadline.

    Returns ``(rearmed, cancelled)`` for observability. Rows with
    ``paused_at IS NULL`` are skipped silently — they shouldn't exist
    (the manager always writes ``paused_at`` alongside the status
    transition) but a defensive check costs nothing.

    Idempotent: once recovery has processed a row, a re-armed row
    that's still within the window will simply re-arm the timer (the
    manager's ``_arm_paused_timer`` cancels any pre-existing timer)
    and a recovered+cancelled row no longer matches the ``paused``
    filter, so a second pass returns ``(0, 0)``.

    ``now`` is overridable so unit tests can pin the wall clock; the
    default reads :func:`datetime.now` in UTC.
    """
    rows = await store.list_paused()
    current = now or datetime.now(UTC)
    rearmed = 0
    cancelled = 0
    for row in rows:
        paused_at = row.get("paused_at")
        if paused_at is None:
            continue
        if paused_at.tzinfo is None:
            paused_at = paused_at.replace(tzinfo=UTC)
        elapsed = (current - paused_at).total_seconds()
        remaining = paused_timeout_s - elapsed
        sid = row["id"]
        if remaining <= 0:
            try:
                await manager.cancel(sid, reason="paused_timeout_recovered")
                cancelled += 1
            except Exception:
                logger.warning(
                    "paused_timeout_recovered cancel failed sid=%s",
                    sid,
                    exc_info=True,
                )
        else:
            try:
                manager._arm_paused_timer(sid, remaining_s=remaining)
                rearmed += 1
            except Exception:
                logger.warning(
                    "paused timer re-arm failed sid=%s remaining=%.2fs",
                    sid,
                    remaining,
                    exc_info=True,
                )
    return rearmed, cancelled
