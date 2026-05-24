"""SqlAlchemy-backed + in-memory DurableEventStore implementations.

Plan 7 D7.17 + Plan 9 D9.9. Both implementations satisfy the
:class:`gg_relay.core.protocol.DurableEventStore` Protocol:

* :class:`SqlAlchemyDurableEventStore` — writes to the ``events``
  table provisioned by Alembic 0004 + extended by Plan 9 ``0012``
  with the ``seq`` BIGSERIAL column. ``persist`` returns the
  strictly-monotonic per-row seq (Postgres
  ``nextval('events_seq_seq') RETURNING`` / SQLite
  ``SELECT COALESCE(MAX(seq), 0) + 1 → INSERT`` inside one
  ``engine.begin()``; the engine's IMMEDIATE txn serialises
  concurrent writers).
* :class:`InMemoryDurableEventStore` — process-local; uses a plain
  incrementing counter + ``asyncio.Lock`` for ordering.

The :class:`ReplayedEvent` dataclass is what
:meth:`SqlAlchemyDurableEventStore.fetch_after` returns when
reconstructing rows: a real :class:`RelayEvent` subclass (so the
SSE filter ``isinstance(event, RelayEvent)`` keeps working) but
carrying the original ``type_name``, ``session_id``, ``payload``,
and ``seq`` in explicit fields. The SSE renderer formats SSE id
as ``"<seq>:<event_id>"`` (no schema_version prefix —
pre-production simplification dropped the v1/v2 dual cursor).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
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
    class name is preserved in ``type_name`` (used by the SSE
    renderer in place of ``type(event).__name__``); ``seq`` is the
    durable store's monotonic cursor so the SSE id is
    ``"<seq>:<event_id>"``.

    Not in :data:`RelayEventT` on purpose — that union is for live
    publisher signatures; replayed events are an SSE-internal shape.
    """

    type_name: str = ""
    session_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    seq: int = 0


def _event_payload(event: RelayEvent) -> dict[str, Any]:
    """Serialise the non-column fields of ``event`` to JSON-ready dict.

    The store writes ``event_id`` / ``ts`` / ``type`` / ``session_id``
    as dedicated columns; everything else (subclass-specific fields
    like ``prompt_redacted``, ``tokens``, ``args_redacted``) goes
    into the ``payload`` JSON blob. Strips the column-backed fields
    out of ``payload`` to avoid duplication.
    """
    raw = dataclasses.asdict(event)
    for stripped in ("event_id", "occurred_at"):
        raw.pop(stripped, None)
    return raw


class SqlAlchemyDurableEventStore:
    """Append-only events table writer + replay reader.

    Conforms to :class:`gg_relay.core.protocol.DurableEventStore`.
    Used in production via the FastAPI lifespan; tests typically
    prefer :class:`InMemoryDurableEventStore` to keep DB-free unit
    coverage.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def persist(self, event: RelayEvent) -> int:
        """Append ``event`` to the events table; return its seq.

        Postgres uses ``nextval('events_seq_seq')`` + RETURNING for
        one round-trip; SQLite uses ``SELECT COALESCE(MAX(seq),0)+1
        → INSERT`` inside the engine.begin() transaction (the
        engine's IMMEDIATE txn serialises concurrent writers so the
        SELECT/INSERT pair is atomic relative to other persisters).
        Other dialects fall through to the SQLite path.
        """
        payload = _event_payload(event)
        type_name = type(event).__name__
        session_id = getattr(event, "session_id", None)
        dialect_name = self._engine.dialect.name
        # Raw text() bypasses SQLAlchemy's JSON adapter — encode
        # payload explicitly for both dialects.
        payload_json = json.dumps(payload, default=str)
        params = {
            "event_id": str(event.event_id),
            "ts": event.occurred_at,
            "type": type_name,
            "session_id": session_id,
            "payload": payload_json,
            "delivery_tier": "disk",
        }
        async with self._engine.begin() as conn:
            if dialect_name == "postgresql":
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
                    params,
                )
                return int(result.scalar_one())
            # SQLite + every other dialect uses MAX(seq)+1.
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
                {**params, "seq": next_seq},
            )
            return next_seq

    async def fetch_after(
        self, *, last_seq: int, limit: int = 1000
    ) -> Sequence[RelayEvent]:
        """Replay events with ``seq > last_seq`` in ascending order."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(events)
                .where(events.c.seq > last_seq)
                .order_by(events.c.seq.asc())
                .limit(limit)
            )
            rows = result.mappings().all()
        return [self._row_to_replayed(row) for row in rows]

    @staticmethod
    def _row_to_replayed(row: Any) -> RelayEvent:
        """Map an `events` row to a :class:`ReplayedEvent`."""
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
        return ReplayedEvent(
            occurred_at=occurred_at,
            delivery_tier=tier,
            type_name=row["type"],
            session_id=row["session_id"] or "",
            payload=payload,
            seq=int(row["seq"]),
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

    @property
    def stored_events(self) -> tuple[RelayEvent, ...]:
        """Snapshot of every persisted event (test-only inspection)."""
        return tuple(event for _, event in self._events)
