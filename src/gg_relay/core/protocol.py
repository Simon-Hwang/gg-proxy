"""Core Protocols — Plan 7 D7.17 (durable EventBus) / Plan 8 D8.1 forward.

Defines the storage surface the optional disk / Redis tier of the
:class:`gg_relay.core.event_bus.EventBus` writes through. The Protocol
lives in ``gg_relay.core`` so the bus itself stays free of SQLAlchemy
and Redis imports — concrete implementations live in:

* :class:`gg_relay.store.durable_event.SqlAlchemyDurableEventStore`
  (production; backs the ``events`` table created by Alembic 0004).
* :class:`gg_relay.store.durable_event.InMemoryDurableEventStore`
  (tests; counter-based monotonic seq, no DB).

Plan 8 will add a ``RedisStreamDurableEventStore`` for the optional
multi-worker tier; the :class:`DurableEventStore` Protocol stays
identical so the bus needs no changes.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from gg_relay.core.events import RelayEvent


@runtime_checkable
class DurableEventStore(Protocol):
    """Persistence backend for the durable tier of :class:`EventBus`.

    Two operations:

    * :meth:`persist` — append a :class:`RelayEvent` to the store and
      return a monotonic ``seq`` integer. ``seq`` is used by the SSE
      ``Last-Event-ID`` cursor (``"<seq>:<event_id>"``) to resume
      replay after a disconnect.
    * :meth:`fetch_after` — yield every event with ``seq > last_seq``
      in ascending order, up to ``limit`` rows. Returning concrete
      :class:`RelayEvent` instances is preferred so the SSE filter
      (``isinstance(event, RelayEvent)``) keeps working; an
      implementation MAY return a subclass that carries additional
      fields (e.g. ``ReplayedEvent`` for SQL reconstruction).

    Implementations MUST be safe to call concurrently from the same
    ``EventBus`` instance — the bus may interleave ``persist`` (from
    publishers) and ``fetch_after`` (from SSE replay) freely.
    """

    async def persist(self, event: RelayEvent) -> int:
        """Append ``event`` to the store and return its monotonic seq."""
        ...

    async def fetch_after(
        self, *, last_seq: int, limit: int = 1000
    ) -> Sequence[RelayEvent]:
        """Replay events with ``seq > last_seq``, ordered ascending."""
        ...


__all__ = ["DurableEventStore"]
