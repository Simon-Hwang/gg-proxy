"""In-process async pub/sub.

The :class:`EventBus` is a single-process fan-out primitive. SessionManager
publishes ``frame``, ``hitl``, ``session_state`` events and any number of
subscribers (Store sink, OTel subscriber, dashboard SSE) consume them via
``async for`` over the iterator returned by :meth:`subscribe`.

Backpressure policy: each subscriber has a bounded deque (default 1000
items). When the deque is full the *oldest* item is dropped to make room
for the new one — telemetry is best-effort and we'd rather lose stale
events than block the publisher. Slow subscribers should monitor
:attr:`dropped_per_topic` and react.

Implementation note: we use ``collections.deque`` + ``asyncio.Event`` rather
than ``asyncio.Queue`` because the close signal must be deliverable even
when the queue is at capacity. The deque model lets us cleanly separate
"new data available" from "stream closed".
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("gg_relay.bus")


@dataclass(slots=True)
class _Sub:
    """Internal per-subscriber state.

    ``items`` is the buffered backlog; ``waker`` fires when a new item
    arrives OR the stream closes. ``closed_flag`` is the terminal signal:
    iterators drain remaining items then exit.
    """

    maxsize: int
    items: deque[Any] = field(default_factory=deque)
    waker: asyncio.Event = field(default_factory=asyncio.Event)
    closed_flag: bool = False
    dropped: int = 0


class EventBus:
    """Topic-keyed async fan-out.

    Subscribers register a topic and receive an async iterator over events.
    Unsubscription happens automatically when the iterator is closed
    (``aclose`` / GC) or when the bus is shut down via :meth:`close`.
    """

    def __init__(self) -> None:
        self._subs: dict[str, list[_Sub]] = {}
        self._closed = False

    @property
    def dropped_per_topic(self) -> dict[str, int]:
        """Snapshot of the per-topic dropped-event counters (summed across
        subscribers)."""
        return {
            topic: sum(s.dropped for s in subs)
            for topic, subs in self._subs.items()
        }

    def subscribe(self, topic: str, *, maxsize: int = 1000) -> AsyncIterator[Any]:
        """Return an async iterator yielding events published to ``topic``.

        Each call returns an independent iterator; multiple subscribers each
        get their own copy. The iterator exits cleanly when :meth:`close`
        is called or when ``aclose()`` is invoked on the iterator (including
        the implicit ``aclose()`` triggered by garbage collection).
        """
        sub = _Sub(maxsize=maxsize)
        self._subs.setdefault(topic, []).append(sub)

        async def _iter() -> AsyncIterator[Any]:
            try:
                while True:
                    while sub.items:
                        yield sub.items.popleft()
                    if sub.closed_flag:
                        return
                    sub.waker.clear()
                    await sub.waker.wait()
            finally:
                bucket = self._subs.get(topic)
                if bucket is not None and sub in bucket:
                    bucket.remove(sub)

        return _iter()

    async def publish(self, topic: str, event: Any) -> None:
        """Fan out ``event`` to every current subscriber of ``topic``.

        Never blocks the publisher; full deques drop their oldest item.
        Publishing on a closed bus is a no-op (logged at debug level).
        """
        if self._closed:
            logger.debug("publish on closed bus dropped event topic=%s", topic)
            return
        for sub in self._subs.get(topic, ()):
            if len(sub.items) >= sub.maxsize:
                sub.items.popleft()
                sub.dropped += 1
            sub.items.append(event)
            sub.waker.set()

    async def close(self) -> None:
        """Signal every subscriber that the bus is shutting down.

        Subscribers drain any buffered events before their iterator exits.
        Safe to call repeatedly — subsequent calls short-circuit.
        """
        if self._closed:
            return
        self._closed = True
        for subs in self._subs.values():
            for sub in subs:
                sub.closed_flag = True
                sub.waker.set()
