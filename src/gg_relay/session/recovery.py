"""Startup recovery — Plan 4 D4.6.

When the relay restarts, any session whose ``status='running'`` belongs to
a previous process that crashed mid-flight. We take the **conservative**
stance (D4.6): the row is marked ``interrupted`` and *not* resumed. The
human operator can decide whether to resubmit; the interrupted row keeps
the original ``spec_json`` + frames so dashboards can render a post-mortem.
"""
from __future__ import annotations

from dataclasses import dataclass

from gg_relay.store import SessionRepository


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
