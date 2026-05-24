"""Core Protocols — Plan 7 D7.17 / Plan 8 D8.1 / Plan 9 v0.9.0-rc D9.0.

Defines the contract surface for the swappable backend tiers of the
event-bus and the rate-limit store. The Protocols live in
``gg_relay.core`` so the bus itself stays free of SQLAlchemy / Redis
imports — concrete implementations live in:

* :class:`gg_relay.store.durable_event.SqlAlchemyDurableEventStore` /
  :class:`gg_relay.store.durable_event.InMemoryDurableEventStore`
  for :class:`DurableEventStore`.
* :class:`gg_relay.core.event_bus.EventBus` (in-memory fan-out)
  satisfies :class:`EventBusBackend` directly; Plan 9.1 will add
  a ``RedisStreamEventBus`` that wires the same Protocol.
* :class:`gg_relay.api.middleware.rate_limit.TokenBucketRateLimiter`
  satisfies :class:`RateLimitStoreBackend` directly; Plan 9.1 will
  add ``RedisRateLimitStore``.

Plan 9 v0.9.0-rc (D9.0) extracts these Protocols as a no-op refactor:
the existing in-memory implementations satisfy them structurally so
zero call-sites change. Plan 9.1 adds Redis-backed variants that
implement the same shape.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any, Protocol, overload, runtime_checkable

if TYPE_CHECKING:
    from gg_relay.core.events import RelayEvent


@runtime_checkable
class DurableEventStore(Protocol):
    """Persistence backend for the durable tier of :class:`EventBus`.

    Three operations (``fetch_after_seq`` added in Plan 9 D9.9a):

    * :meth:`persist` — append a :class:`RelayEvent` to the store and
      return a monotonic ``seq`` integer. The cursor format on the
      wire is governed by Plan 9 D9.9a (``api/sse.py``):
      v0.8.x ships a microsecond timestamp; v0.9.0+ may emit a
      ``"v2:<row-seq>"`` form once D9.9 Alembic 0012a ships an
      `events.seq BIGSERIAL`. ``persist`` itself stays Protocol-agnostic
      about cursor format — it always returns the implementation's
      native monotonic int.
    * :meth:`fetch_after` — replay events with `microsecond-cursor >
      last_seq`. Kept for backward compatibility with v0.8.x clients
      whose `Last-Event-ID` is a microsecond timestamp.
    * :meth:`fetch_after_seq` — Plan 9 D9.9a — replay events with
      `events.seq > last_seq`. The SSE router dispatches between the
      two `fetch_after*` paths based on the cursor's `schema_version`
      prefix (``v2:`` → row-seq path, otherwise → microsecond path).

    Implementations MUST be safe to call concurrently from the same
    ``EventBus`` instance — the bus may interleave ``persist`` and
    either ``fetch_after*`` variant freely.
    """

    async def persist(self, event: RelayEvent) -> int:
        """Append ``event`` to the store and return its monotonic seq."""
        ...

    async def fetch_after(
        self, *, last_seq: int, limit: int = 1000
    ) -> Sequence[RelayEvent]:
        """Replay events with microsecond-cursor > ``last_seq``."""
        ...

    async def fetch_after_seq(
        self, *, last_seq: int, limit: int = 1000
    ) -> Sequence[RelayEvent]:
        """Plan 9 D9.9a — replay events with ``events.seq > last_seq``.

        Default behaviour for in-memory implementations (where the
        microsecond timestamp and row-seq are equivalent) is to
        delegate to :meth:`fetch_after`; SQL implementations override
        with a true ``WHERE seq > :n`` predicate after Alembic 0012a
        ships the column.
        """
        ...


@runtime_checkable
class EventBusBackend(Protocol):
    """Pluggable event-bus backend (Plan 9 v0.9.0-rc D9.0).

    Two-method design (Santa Round 3 Reviewer F BLOCKER #1):

    * :meth:`subscribe` — existing topic-based fan-out used by ~17
      call sites (``api/sse.py``, ``im/subscriber.py``,
      ``tracing/metrics_subscriber.py``, etc.). Returns an async
      iterator scoped to a single topic (typed class, class-name
      string, legacy string, or ``"*"`` wildcard).
    * :meth:`publish` — overloaded for the canonical typed form and
      the legacy 2-arg ``(topic_str, payload)`` form. Matches
      :meth:`EventBus.publish` exactly.
    * :meth:`subscribe_all` — Plan 9.1 forward — durable replay /
      cross-worker fan-out using a single ``events.seq`` cursor.
      Default in-memory implementation derives this from the bus's
      attached :class:`DurableEventStore`.

    The existing :class:`gg_relay.core.event_bus.EventBus` satisfies
    this Protocol structurally — no constructor change, no caller
    change. Plan 9.1 will add ``RedisStreamEventBus`` that implements
    the same shape so the lifespan can swap backends from config.
    """

    def subscribe(
        self,
        topic: type[RelayEvent] | str,
        *,
        maxsize: int = 1000,
    ) -> AsyncIterator[Any]:
        """Topic-keyed async fan-out."""
        ...

    @overload
    async def publish(self, topic_or_event: RelayEvent, /) -> None: ...
    @overload
    async def publish(self, topic_or_event: str, event: Any, /) -> None: ...

    async def publish(
        self,
        topic_or_event: RelayEvent | str,
        event: Any = None,
        /,
    ) -> None:
        """Fan an event out to subscribers (typed or legacy 2-arg form)."""
        ...

    def subscribe_all(
        self,
        *,
        after_seq: int | None = None,
    ) -> AsyncIterator[RelayEvent]:
        """Plan 9 D9.0 — cross-worker durable replay.

        Yields every event with ``seq > after_seq`` in monotonic order.
        ``after_seq=None`` is reserved for "start at end" semantics
        used by fresh SSE subscribers that don't want a replay.
        In-memory implementations may treat ``None`` the same as the
        store's current head; Redis implementations use ``$`` (XREAD
        from-end).
        """
        ...

    async def close(self) -> None:
        """Drain and tear down every subscriber."""
        ...


@runtime_checkable
class RateLimitStoreBackend(Protocol):
    """Pluggable rate-limit backend (Plan 9 v0.9.0-rc D9.0).

    Distinct from :class:`gg_relay.api.middleware.rate_limit.RateLimitMiddleware`
    (which is the Starlette adapter) — the backend owns the bucket
    state only. The existing
    :class:`gg_relay.api.middleware.rate_limit.TokenBucketRateLimiter`
    satisfies this Protocol structurally so the swap to Plan 9.1
    ``RedisRateLimitStore`` is local to the lifespan.

    ``acquire`` is the only required method (`start_sweep` and `stop`
    are convenience hooks on the in-memory impl; Redis impl will be
    fire-and-forget).
    """

    async def acquire(self, key: str) -> tuple[bool, float]:
        """Try to spend one token for ``key``.

        Returns ``(allowed, retry_after_seconds)``. ``retry_after`` is
        ``0`` when allowed; otherwise the time until ``key`` has at
        least one token again.
        """
        ...


__all__ = [
    "DurableEventStore",
    "EventBusBackend",
    "RateLimitStoreBackend",
]
