"""SqlAlchemy-backed + in-memory DurableEventStore implementations.

Plan 7 D7.17 (Task 13). Both implementations satisfy the
:class:`gg_relay.core.protocol.DurableEventStore` Protocol:

* :class:`SqlAlchemyDurableEventStore` — writes to the ``events`` table
  provisioned by Alembic 0004. The monotonic ``seq`` returned by
  :meth:`persist` is the event's ``occurred_at`` cast to microseconds
  since epoch; ``(ts, event_id)`` is the tiebreaker when two events
  share a microsecond (which is rare in practice but cross-dialect
  safe). Plan 8 will swap this for a native ``bigserial`` column or
  the Redis stream id once the multi-worker tier lands.
* :class:`InMemoryDurableEventStore` — used in unit tests and the SSE
  integration test. Uses a plain incrementing counter and an
  ``asyncio.Lock`` for cross-task ordering.

The :class:`ReplayedEvent` dataclass is what
:meth:`SqlAlchemyDurableEventStore.fetch_after` returns when
reconstructing rows: it's a real :class:`RelayEvent` subclass (so the
SSE filter ``isinstance(event, RelayEvent)`` keeps working) but carries
the original ``type_name``, ``session_id``, ``payload``, and ``seq`` in
explicit fields so the SSE renderer can format the
``Last-Event-ID: <seq>:<event_id>`` cursor without guessing.
"""
from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.core.events import RelayEvent
from gg_relay.store.schema import events

__all__ = [
    "InMemoryDurableEventStore",
    "ReplayedEvent",
    "SqlAlchemyDurableEventStore",
]


@dataclass(frozen=True, slots=True)
class ReplayedEvent(RelayEvent):
    """Reconstructed event yielded by SQL-backed ``fetch_after``.

    Inherits from :class:`RelayEvent` so SSE generators / IM
    subscribers that filter on ``isinstance(event, RelayEvent)`` see
    replayed events the same as live ones. The original wire-level
    class name is preserved in ``type_name`` (used by the SSE renderer
    in place of ``type(event).__name__``); ``seq`` is the durable
    store's monotonic cursor so the SSE id is ``"<seq>:<event_id>"``.

    Not in :data:`RelayEventT` on purpose — that union is for live
    publisher signatures; replayed events are an SSE-internal shape.
    """

    type_name: str = ""
    session_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    seq: int = 0


def _event_seq(event: RelayEvent) -> int:
    """Microsecond-since-epoch seq for the SQL store.

    Cross-dialect monotonicity: SQLite has no native bigserial and our
    Alembic 0004 table uses a string event_id PK, so we derive seq from
    ``occurred_at`` (a tz-aware datetime defaulting to ``now()`` on
    construction). Two events at the same microsecond will collide on
    seq; ``fetch_after`` orders by ``(ts, event_id)`` so the SSE replay
    still walks them in a deterministic order. Plan 8 RedisStream tier
    will replace this with a native stream id.
    """
    occurred = getattr(event, "occurred_at", None)
    if occurred is None:
        occurred = datetime.now(UTC)
    return int(occurred.timestamp() * 1_000_000)


def _event_payload(event: RelayEvent) -> dict[str, Any]:
    """Serialise the non-column fields of ``event`` to JSON-ready dict.

    The store writes ``event_id`` / ``ts`` / ``type`` / ``session_id``
    as dedicated columns; everything else (subclass-specific fields
    like ``prompt_redacted``, ``tokens``, ``args_redacted``) goes into
    the ``payload`` JSON blob. Strips the column-backed fields out of
    ``payload`` to avoid duplication.
    """
    raw = dataclasses.asdict(event)
    for stripped in ("event_id", "occurred_at"):
        raw.pop(stripped, None)
    return raw


class SqlAlchemyDurableEventStore:
    """Append-only events table writer + replay reader.

    Conforms to :class:`gg_relay.core.protocol.DurableEventStore`. Used
    in production via the FastAPI lifespan; tests typically prefer
    :class:`InMemoryDurableEventStore` to keep DB-free unit coverage.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def persist(self, event: RelayEvent) -> int:
        seq = _event_seq(event)
        payload = _event_payload(event)
        type_name = type(event).__name__
        session_id = getattr(event, "session_id", None)
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(events).values(
                    event_id=str(event.event_id),
                    ts=event.occurred_at,
                    type=type_name,
                    session_id=session_id,
                    payload=payload,
                    delivery_tier="disk",
                )
            )
        return seq

    async def fetch_after(
        self, *, last_seq: int, limit: int = 1000
    ) -> Sequence[RelayEvent]:
        # Convert the seq cursor back into a microsecond-precise
        # datetime; the schema stores tz-aware UTC ts, so we recover
        # the same wall-clock instant the writer used.
        cutoff = datetime.fromtimestamp(last_seq / 1_000_000, tz=UTC)
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(events)
                .where(events.c.ts > cutoff)
                .order_by(events.c.ts.asc(), events.c.event_id.asc())
                .limit(limit)
            )
            rows = result.mappings().all()
        out: list[RelayEvent] = []
        for row in rows:
            ts_value = row["ts"]
            occurred_at = (
                ts_value
                if isinstance(ts_value, datetime)
                else datetime.now(UTC)
            )
            payload = dict(row["payload"]) if row["payload"] is not None else {}
            tier_value = payload.get("delivery_tier")
            tier: Any = (
                tier_value if tier_value in ("lossy", "durable") else "durable"
            )
            out.append(
                ReplayedEvent(
                    occurred_at=occurred_at,
                    delivery_tier=tier,
                    type_name=row["type"],
                    session_id=row["session_id"] or "",
                    payload=payload,
                    seq=int(occurred_at.timestamp() * 1_000_000),
                )
            )
        return out


class InMemoryDurableEventStore:
    """Process-local Durable store used by unit tests.

    Uses a plain incrementing counter for ``seq`` (1-based) and an
    ``asyncio.Lock`` to keep ordering deterministic when multiple
    tasks publish concurrently. Returns the original
    :class:`RelayEvent` instances on ``fetch_after`` — no
    reconstruction needed because the store owns the references.
    """

    def __init__(self) -> None:
        self._events: list[tuple[int, RelayEvent]] = []
        self._lock = asyncio.Lock()
        self._next_seq = 1

    async def persist(self, event: RelayEvent) -> int:
        async with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            self._events.append((seq, event))
        return seq

    async def fetch_after(
        self, *, last_seq: int, limit: int = 1000
    ) -> Sequence[RelayEvent]:
        async with self._lock:
            window = [event for seq, event in self._events if seq > last_seq]
        return window[:limit]

    @property
    def stored_events(self) -> tuple[RelayEvent, ...]:
        """Snapshot of every persisted event (test-only inspection)."""
        return tuple(event for _, event in self._events)
