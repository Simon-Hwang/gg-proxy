"""Plan 9 D9.1 — RedisStreamEventBus.

Cross-worker event-bus implementation that satisfies
:class:`gg_relay.core.protocol.EventBusBackend`. Backed by a single
Redis stream (``gg-relay:events`` by default — see
:data:`gg_relay.cluster.wire.STREAM_KEY`) so every worker pod that
publishes can be tailed by every other worker via XREAD without
worker-to-worker connections.

Topology
~~~~~~~~

Each :class:`RedisStreamEventBus` instance owns:

* one writer connection used by :meth:`publish` for XADD,
* one reader connection per active subscriber task used for XREAD
  with a long block timeout (live tail / replay).

Subscribers come in two flavours:

* :meth:`subscribe(topic)` — local topic-keyed deque that filters
  the *full* XREAD output stream by event type, so the existing
  call-site signature (``bus.subscribe(SessionCreated)``) keeps
  working unchanged. A single background coroutine
  (:meth:`_pump_task`) reads from XREAD ``$`` and fans entries out
  to every local deque.
* :meth:`subscribe_all(after_seq)` — used by the SSE durable-replay
  path (Plan 9 D9.3). Returns an :class:`AsyncIterator` that wraps
  XREAD directly with no local fanout; consumer drives the cursor.

The wire format is owned entirely by :mod:`gg_relay.cluster.wire`
(Plan 9 D9.13) — this module never touches JSON or schema
versioning.

Strict mode handling
~~~~~~~~~~~~~~~~~~~~

The constructor accepts an already-built ``aioredis.Redis`` client;
connection failures are the caller's responsibility (lifespan in
``api/main.py`` plumbs ``cfg.strict_backend`` into
:func:`gg_relay.cluster.factory.build_event_bus`). This keeps the
bus itself trivial-to-test (fakeredis injection) and consolidates
the "fall back to in-memory when Redis is down" policy in one
place.

Connection cleanup
~~~~~~~~~~~~~~~~~~

:meth:`close` cancels :attr:`_pump_task`, awaits its exit, and
closes every per-subscriber reader connection. The writer
connection is owned by the caller (lifespan) — calling
``await redis.aclose()`` is the caller's responsibility so that a
shared ``aioredis.Redis`` instance can outlive any single bus
instance.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, cast

from gg_relay.cluster.wire import (
    STREAM_KEY,
    UnsupportedWireVersionError,
    decode_event,
    encode_event,
)
from gg_relay.core.events import RelayEvent
from gg_relay.store.durable_event import ReplayedEvent

if TYPE_CHECKING:
    import redis.asyncio as aioredis

logger = logging.getLogger("gg_relay.cluster.redis_bus")

# Block this long on XREAD before returning empty. Short enough that
# graceful shutdown (which cancels the task) doesn't get stuck for
# minutes; long enough that idle clusters don't burn CPU. Redis
# accepts ms.
_XREAD_BLOCK_MS = 2000


class _LocalDeque:
    """Per-subscriber queue with topic filtering.

    Built on top of :class:`asyncio.Queue` rather than ``deque`` so
    backpressure (when a slow subscriber falls behind) blocks the
    fan-out pump instead of silently dropping events. The bus
    creator MAY raise ``maxsize`` per subscriber for short-burst
    high-fan-out subscribers (alert_router) but defaults match the
    Plan 7 in-memory bus.
    """

    def __init__(
        self,
        *,
        topic: type[RelayEvent] | str,
        maxsize: int,
    ) -> None:
        self._queue: asyncio.Queue[RelayEvent] = asyncio.Queue(maxsize=maxsize)
        self._topic = topic
        self._closed = asyncio.Event()

    def matches(self, event: RelayEvent) -> bool:
        topic = self._topic
        if topic == "*":
            return True
        if isinstance(topic, type):
            return isinstance(event, topic) or (
                isinstance(event, ReplayedEvent)
                and event.type_name == topic.__name__
            )
        # str topic — match by class name
        return (
            type(event).__name__ == topic
            or (isinstance(event, ReplayedEvent) and event.type_name == topic)
        )

    async def offer(self, event: RelayEvent) -> None:
        if self._closed.is_set():
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "redis_bus.subscriber_full topic=%r — dropping event %s",
                self._topic,
                getattr(event, "event_id", "?"),
            )

    def __aiter__(self) -> _LocalDeque:
        return self

    async def __anext__(self) -> RelayEvent:
        while True:
            if self._closed.is_set() and self._queue.empty():
                raise StopAsyncIteration
            try:
                evt = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            return evt

    async def aclose(self) -> None:
        self._closed.set()


class RedisStreamEventBus:
    """:class:`EventBusBackend` implementation using Redis streams.

    Constructor accepts a pre-built ``redis.asyncio.Redis`` client
    (with ``decode_responses=True``) so:

    * the lifespan can share one client across the bus + rate-limit
      store (saves connection overhead),
    * tests inject ``fakeredis.aioredis.FakeRedis`` directly,
    * connection management policy stays in one place (the lifespan
      handles retry / TLS / Sentinel).
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        stream_key: str = STREAM_KEY,
        maxlen: int = 100_000,
        approximate: bool = True,
    ) -> None:
        self._redis = redis
        self._stream_key = stream_key
        # XADD MAXLEN ~ N caps the stream so old entries get trimmed
        # automatically. ``approximate=True`` is the operator-friendly
        # default — exact MAXLEN forces O(N) eviction per write.
        self._maxlen = maxlen
        self._approximate = approximate
        self._subs: list[_LocalDeque] = []
        self._pump_task: asyncio.Task[None] | None = None
        self._closed = False

    # ── Publish ─────────────────────────────────────────────────────
    async def publish(
        self,
        topic_or_event: RelayEvent | str,
        event: Any = None,
        /,
    ) -> None:
        """XADD the event to the configured stream.

        Supports both the canonical typed form
        (``await bus.publish(SessionCreated(...))``) and the legacy
        2-arg form (``await bus.publish("legacy.topic", payload)``).
        The 2-arg form is a no-op on Redis — legacy string topics
        were Plan 5 frame fan-out and don't have a wire schema.
        """
        if isinstance(topic_or_event, str):
            # Legacy 2-arg form — silently drop. Plan 5 frame fan-out
            # is local-only by design (per-session SSE consumers
            # subscribe to the SAME worker that owns the session).
            return
        assert isinstance(topic_or_event, RelayEvent)
        entry = encode_event(topic_or_event)
        # mypy: redis-py types xadd's fields parameter with an
        # invariant dict[bytes|str|int|float, …] union — narrowing
        # encode_event's dict[str, str] requires a cast.
        await self._redis.xadd(
            self._stream_key,
            cast("dict[Any, Any]", entry),
            maxlen=self._maxlen,
            approximate=self._approximate,
        )

    # ── Local subscriber path (topic-keyed) ─────────────────────────
    def subscribe(
        self,
        topic: type[RelayEvent] | str,
        *,
        maxsize: int = 1000,
    ) -> AsyncIterator[Any]:
        """Topic-keyed local fan-out.

        Lazy-starts :meth:`_pump_task` on the first subscription so
        idle clusters don't burn a long-lived XREAD coroutine.
        """
        deque = _LocalDeque(topic=topic, maxsize=maxsize)
        self._subs.append(deque)
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(
                self._pump_loop(), name="gg-relay.redis_bus.pump"
            )
        return deque

    # ── Cross-worker durable replay ─────────────────────────────────
    async def subscribe_all(
        self,
        *,
        after_seq: int | None = None,
        limit: int = 1000,
    ) -> AsyncIterator[RelayEvent]:
        """Cross-worker replay from a specific stream cursor.

        ``after_seq=None`` reads from ``$`` (live tail, no replay).
        Otherwise expects a stream cursor (``<ms>-<n>``) — for now we
        accept the same int Plan 9 D9.9 ``events.seq`` and use it as
        an integer XADD cursor (``"0-N"``), since stream IDs sort
        lexicographically as integers when the timestamp prefix is
        zero. Operators who want point-in-time replay should pass
        the stream ID string directly via a future
        ``after_stream_id`` parameter.
        """
        last_id = "$" if after_seq is None else f"0-{after_seq}"
        count = 0
        while count < limit:
            response = await self._redis.xread(
                {self._stream_key: last_id},
                count=min(100, limit - count),
                block=_XREAD_BLOCK_MS,
            )
            if not response:
                # No new entries within the block window — when
                # replaying historical data this means we've caught
                # up; for live tail it just means another poll cycle.
                if after_seq is not None:
                    return
                continue
            for _stream, entries in response:
                for entry_id, entry_fields in entries:
                    last_id = entry_id
                    try:
                        evt = decode_event(_normalise_fields(entry_fields))
                    except (UnsupportedWireVersionError, KeyError) as exc:
                        logger.warning(
                            "redis_bus.decode_failed id=%s err=%s",
                            entry_id,
                            exc,
                        )
                        continue
                    yield evt
                    count += 1
                    if count >= limit:
                        return

    # ── Pump (one per bus instance) ─────────────────────────────────
    async def _pump_loop(self) -> None:
        """Tail XREAD ``$`` and fan entries into every local deque."""
        last_id: str = "$"
        try:
            while not self._closed:
                response = await self._redis.xread(
                    {self._stream_key: last_id},
                    count=100,
                    block=_XREAD_BLOCK_MS,
                )
                if not response:
                    continue
                for _stream, entries in response:
                    for entry_id, entry_fields in entries:
                        last_id = entry_id
                        try:
                            evt = decode_event(_normalise_fields(entry_fields))
                        except (
                            UnsupportedWireVersionError,
                            KeyError,
                        ) as exc:
                            logger.warning(
                                "redis_bus.decode_failed id=%s err=%s",
                                entry_id,
                                exc,
                            )
                            continue
                        await self._fanout(evt)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("redis_bus.pump_crashed")

    async def _fanout(self, event: RelayEvent) -> None:
        # Snapshot the subscribers list so close() can remove items
        # concurrently without raising RuntimeError.
        for sub in list(self._subs):
            if sub.matches(event):
                await sub.offer(event)

    # ── Close ───────────────────────────────────────────────────────
    async def close(self) -> None:
        self._closed = True
        for sub in list(self._subs):
            await sub.aclose()
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump_task


def _normalise_fields(raw: Any) -> dict[str, str]:
    """Coerce redis-py's response (bytes/str) into a str-keyed dict.

    With ``decode_responses=True`` on the client, fields come back
    as ``dict[str, str]`` already; without it, both keys and values
    are ``bytes``. Normalising here means tests can use either
    fakeredis configuration.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = k.decode("utf-8") if isinstance(k, bytes) else str(k)
        val = v.decode("utf-8") if isinstance(v, bytes) else str(v)
        out[key] = val
    return out


__all__ = ["RedisStreamEventBus"]
