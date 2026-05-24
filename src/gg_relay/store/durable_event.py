"""SqlAlchemy-backed + in-memory DurableEventStore implementations.

Plan 7 D7.17 (Task 13). Both implementations satisfy the
:class:`gg_relay.core.protocol.DurableEventStore` Protocol:

* :class:`SqlAlchemyDurableEventStore` — writes to the ``events`` table
  provisioned by Alembic 0004 (and extended by Plan 9 0012a with the
  new ``seq`` column). Plan 9 v0.9.0-rc D9.9 — ``persist`` now fills
  the new ``events.seq`` BIGINT column with a strictly-monotonic per-row
  sequence: Postgres uses ``nextval('events_seq_seq') RETURNING``
  (one round-trip); SQLite uses ``SELECT COALESCE(MAX(seq),0)+1`` →
  ``INSERT`` inside a single ``engine.begin()`` transaction (no
  RETURNING dependency, so it works on the SQLite-3.26 bundled with
  stock Python builds; the engine's IMMEDIATE txn serialises
  concurrent writers so the SELECT/INSERT pair is atomic). Both
  dialects fall back to the legacy Plan 7 microsecond-derived seq on
  any exception, keeping pods green while operators schedule the
  Alembic 0012a migration window.
* :class:`InMemoryDurableEventStore` — used in unit tests and the SSE
  integration test. Uses a plain incrementing counter and an
  ``asyncio.Lock`` for cross-task ordering.

The :class:`ReplayedEvent` dataclass is what
:meth:`SqlAlchemyDurableEventStore.fetch_after` returns when
reconstructing rows: it's a real :class:`RelayEvent` subclass (so the
SSE filter ``isinstance(event, RelayEvent)`` keeps working) but carries
the original ``type_name``, ``session_id``, ``payload``, and ``seq`` in
explicit fields so the SSE renderer can format the
``Last-Event-ID: v2:<seq>:<event_id>`` (Plan 9 D9.9a) or
``Last-Event-ID: <seq>:<event_id>`` (v0.8.x compat) cursor without
guessing.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, select, text
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
        """Append ``event`` to the events table and return its seq.

        Plan 9 v0.9.0-rc D9.9 — the returned seq is now sourced from
        the database (Postgres ``events_seq_seq`` sequence /
        SQLite ``COALESCE(MAX(seq),0)+1``) rather than the wall-clock
        microsecond timestamp. This makes the v0.9.1 D9.9a v2 SSE
        cursor (``Last-Event-ID: v2:<row-seq>``) a reliable strictly-
        monotonic integer instead of one that can collide when two
        events share a microsecond.

        Fallback: if Alembic 0012a hasn't run yet (the ``seq`` column
        is missing or the sequence object is absent), we silently
        derive seq from the microsecond timestamp the way Plan 7
        D7.17 did — keeping v0.9.0-rc safe to deploy against a
        pre-0012a database while the operator schedules the
        migration window.
        """
        payload = _event_payload(event)
        type_name = type(event).__name__
        session_id = getattr(event, "session_id", None)
        dialect_name = self._engine.dialect.name
        # JSON columns map to TEXT on SQLite; raw text() bypasses the
        # SQLAlchemy type adapter so we json.dumps explicitly. Postgres
        # JSONB accepts the same encoded string fine.
        payload_json = json.dumps(payload, default=str)
        if dialect_name == "postgresql":
            try:
                async with self._engine.begin() as conn:
                    result = await conn.execute(
                        text(
                            "INSERT INTO events "
                            "(event_id, ts, type, session_id, payload, "
                            " delivery_tier, seq) "
                            "VALUES (:event_id, :ts, :type, :session_id, "
                            "        :payload, :delivery_tier, "
                            "        nextval('events_seq_seq')) "
                            "RETURNING seq"
                        ),
                        {
                            "event_id": str(event.event_id),
                            "ts": event.occurred_at,
                            "type": type_name,
                            "session_id": session_id,
                            "payload": payload_json,
                            "delivery_tier": "disk",
                        },
                    )
                    return int(result.scalar_one())
            except Exception:
                pass
        elif dialect_name == "sqlite":
            try:
                async with self._engine.begin() as conn:
                    max_seq = (
                        await conn.execute(
                            text("SELECT COALESCE(MAX(seq), 0) FROM events")
                        )
                    ).scalar_one()
                    next_seq = int(max_seq) + 1
                    await conn.execute(
                        text(
                            "INSERT INTO events "
                            "(event_id, ts, type, session_id, payload, "
                            " delivery_tier, seq) VALUES "
                            "(:event_id, :ts, :type, :session_id, "
                            " :payload, :delivery_tier, :seq)"
                        ),
                        {
                            "event_id": str(event.event_id),
                            "ts": event.occurred_at,
                            "type": type_name,
                            "session_id": session_id,
                            "payload": payload_json,
                            "delivery_tier": "disk",
                            "seq": next_seq,
                        },
                    )
                    return next_seq
            except Exception:
                pass
        # Fallback: pre-0012a (no seq column or sequence) or unknown
        # dialect. Use the legacy microsecond-derived seq via a plain
        # SQLAlchemy Core insert (no seq column written — DB stores
        # NULL there).
        fallback_seq = _event_seq(event)
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
        return fallback_seq

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
        return [self._row_to_replayed(row) for row in rows]

    async def fetch_after_seq(
        self, *, last_seq: int, limit: int = 1000
    ) -> Sequence[RelayEvent]:
        """Plan 9 D9.9a — replay with `events.seq > last_seq`.

        After Alembic 0012b, `events.seq` is NOT NULL and uniquely
        indexed. v0.9.0-rc ships 0012a only (nullable column); rows
        written by old v0.8.x pods during a rolling deploy may have
        NULL seq. We coalesce NULL to 0 so backward-compat reads
        still walk the table, and order falls back to `(ts, event_id)`
        for the NULL bucket so ordering remains deterministic.
        """
        from sqlalchemy import func  # local import — Plan 9 only

        async with self._engine.begin() as conn:
            # Use raw SQL with COALESCE for the cursor predicate; the
            # SQLAlchemy Core expression for COALESCE-of-column-vs-int
            # in a WHERE clause is more verbose than the equivalent
            # text() and produces identical SQL on both dialects.
            result = await conn.execute(
                select(events)
                .where(func.coalesce(events.c.seq, 0) > last_seq)
                .order_by(
                    func.coalesce(events.c.seq, 0).asc(),
                    events.c.ts.asc(),
                    events.c.event_id.asc(),
                )
                .limit(limit)
            )
            rows = result.mappings().all()
        return [self._row_to_replayed(row) for row in rows]

    @staticmethod
    def _row_to_replayed(row: Any) -> RelayEvent:
        """Map an `events` row to a :class:`ReplayedEvent`.

        Prefers the `seq` column (Plan 9 0012a+) when present;
        falls back to the microsecond-derived seq for compatibility
        with pre-0012a rows. The seq field on ReplayedEvent is what
        downstream SSE renderers format into `Last-Event-ID`.
        """
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
        row_seq = row.get("seq") if hasattr(row, "get") else None
        if row_seq is None:
            # Pre-Alembic-0012a fallback — derive from microsecond ts.
            row_seq = int(occurred_at.timestamp() * 1_000_000)
        return ReplayedEvent(
            occurred_at=occurred_at,
            delivery_tier=tier,
            type_name=row["type"],
            session_id=row["session_id"] or "",
            payload=payload,
            seq=int(row_seq),
        )


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

    async def fetch_after_seq(
        self, *, last_seq: int, limit: int = 1000
    ) -> Sequence[RelayEvent]:
        """Plan 9 D9.9a — same as `fetch_after` for the in-memory store.

        The in-memory counter `seq` IS the row-seq, so the v2 path
        is identical to the v1 path. SQL implementations diverge
        because the microsecond cursor and the row-seq cursor refer
        to different columns.
        """
        return await self.fetch_after(last_seq=last_seq, limit=limit)

    @property
    def stored_events(self) -> tuple[RelayEvent, ...]:
        """Snapshot of every persisted event (test-only inspection)."""
        return tuple(event for _, event in self._events)
