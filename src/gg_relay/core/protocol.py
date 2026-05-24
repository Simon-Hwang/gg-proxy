"""Core Protocols — Plan 7 D7.17 / Plan 9 v0.9.0 forward.

Defines the contract surface for the swappable backend tiers of the
event-bus and the rate-limit store. The Protocols live in
``gg_relay.core`` so the bus itself stays free of SQLAlchemy / Redis
imports — concrete implementations live in:

* :class:`gg_relay.store.durable_event.SqlAlchemyDurableEventStore` /
  :class:`gg_relay.store.durable_event.InMemoryDurableEventStore`
  for :class:`DurableEventStore`.
* :class:`gg_relay.core.event_bus.EventBus` (in-memory fan-out)
  satisfies :class:`EventBusBackend` directly;
  :class:`gg_relay.cluster.redis_bus.RedisStreamEventBus` is the
  multi-worker variant.
* :class:`gg_relay.api.middleware.rate_limit.TokenBucketRateLimiter`
  satisfies :class:`RateLimitStoreBackend` for single-worker;
  :class:`gg_relay.cluster.redis_rate_limit.RedisRateLimitStore` is
  the multi-worker variant.

Cursor model (post-v0.9.0 simplification):

Pre-production we dropped the dual-version cursor (Plan 9 v1.4
shipped v1 microsecond + v2 row-seq compat for "rolling upgrade"
safety). Since gg-relay had no installed userbase, the v1
microsecond path was net-negative complexity. ``persist`` returns
the monotonic ``events.seq`` (BIGSERIAL); ``fetch_after`` walks the
same column; SSE ``Last-Event-ID`` is ``"<seq>:<event_id>"`` (no
schema_version prefix).
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any, Protocol, overload, runtime_checkable

if TYPE_CHECKING:
    from gg_relay.core.events import RelayEvent


@runtime_checkable
class DurableEventStore(Protocol):
    """Persistence backend for the durable tier of :class:`EventBus`.

    Two operations:

    * :meth:`persist` — append a :class:`RelayEvent` to the store and
      return its monotonic ``events.seq`` (BIGSERIAL). The SSE
      ``Last-Event-ID`` cursor (``"<seq>:<event_id>"``) uses this
      value to resume replay after a disconnect.
    * :meth:`fetch_after` — yield every event with ``seq > last_seq``
      in ascending order, up to ``limit`` rows. Implementations MAY
      return a subclass that carries additional fields (e.g.
      :class:`ReplayedEvent` for SQL reconstruction).

    Implementations MUST be safe to call concurrently from the same
    ``EventBus`` instance — the bus may interleave ``persist`` and
    ``fetch_after`` freely.
    """

    async def persist(self, event: RelayEvent) -> int:
        """Append ``event`` to the store and return its monotonic seq."""
        ...

    async def fetch_after(
        self, *, last_seq: int, limit: int = 1000
    ) -> Sequence[RelayEvent]:
        """Replay events with ``seq > last_seq``, ordered ascending."""
        ...


@runtime_checkable
class EventBusBackend(Protocol):
    """Pluggable event-bus backend (Plan 9 D9.0).

    Three methods cover every existing call site:

    * :meth:`subscribe` — topic-based fan-out (typed class,
      class-name string, legacy string, or ``"*"`` wildcard).
    * :meth:`publish` — overloaded for the canonical typed form and
      the legacy 2-arg ``(topic_str, payload)`` form.
    * :meth:`subscribe_all` — cross-worker durable replay using the
      single ``events.seq`` cursor. The single-worker
      :class:`EventBus` derives this from its attached
      :class:`DurableEventStore`; the multi-worker
      :class:`RedisStreamEventBus` derives this from XREAD on
      ``gg-relay:events``.
    * :meth:`close` — drain + tear down every subscriber.
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
        """Cross-worker durable replay.

        Yields every event with ``seq > after_seq`` in monotonic
        order. ``after_seq=None`` means "start at end" (no replay,
        live tail only). Redis implementations use XREAD ``$`` for
        the None case; in-memory implementations yield nothing.
        """
        ...

    async def close(self) -> None:
        """Drain and tear down every subscriber."""
        ...


@runtime_checkable
class RateLimitStoreBackend(Protocol):
    """Pluggable rate-limit backend (Plan 9 D9.0).

    Distinct from
    :class:`gg_relay.api.middleware.rate_limit.RateLimitMiddleware`
    (the Starlette adapter) — the backend owns the bucket state
    only.

    Single-worker:
    :class:`gg_relay.api.middleware.rate_limit.TokenBucketRateLimiter`.
    Multi-worker:
    :class:`gg_relay.cluster.redis_rate_limit.RedisRateLimitStore`
    (Lua-atomic).
    """

    async def acquire(self, key: str) -> tuple[bool, float]:
        """Try to spend one token for ``key``.

        Returns ``(allowed, retry_after_seconds)``. ``retry_after``
        is ``0`` when allowed; otherwise the time until ``key`` has
        at least one token again.
        """
        ...


__all__ = [
    "DurableEventStore",
    "EventBusBackend",
    "RateLimitStoreBackend",
]
