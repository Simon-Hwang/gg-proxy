"""In-process async pub/sub (Plan 5 D5.2=A3 + D5.3 delivery-tier policy).

Two publish forms are accepted:

- ``publish(event: RelayEvent)`` — canonical typed form. The topic is
  ``type(event).__name__`` (e.g. ``SessionCreated``); subscribers register
  either by the dataclass type or by the same class-name string.
- ``publish(topic: str, payload: Any)`` — legacy 2-arg form, retained for
  back-compat with existing string-topic subscribers (``frame`` /
  ``session_state`` / ``hitl``). Slated for removal once every subscriber
  consumes typed events.

Subscription mirrors the duality::

    bus.subscribe(SessionStateChanged)       # typed
    bus.subscribe("SessionStateChanged")     # equivalent
    bus.subscribe("frame")                    # legacy

Wildcard subscriptions use the special key ``"*"`` and receive every event
regardless of topic — useful for the SSE/IM/task-trace subscribers that
filter per-session inside the consumer.

Backpressure / delivery tier (D5.3):

The tier on each :class:`RelayEvent` is **a hint to the dispatcher about
how to react when a subscriber queue is full**, not a persistence
contract. Persistence is handled by the SessionManager pipeline *before*
publish — every frame and HITL request is already on disk by the time a
subscriber sees the event.

* ``lossy`` events (telemetry: SessionOutputChunk, Heartbeat, InstallDone):
  full queue → drop the oldest item to make room, increment the per-topic
  drop counter. Never blocks the publisher. Subscribers that miss an
  event can catch up via the SSE Last-Event-ID back-fill (Task 3).
* ``durable`` events (control: SessionCreated, SessionStateChanged,
  SessionCompleted, HITLRequested, HITLResolved, ToolRequested,
  ToolResolved, InstallError): full queue → publisher awaits the
  per-subscriber "drained" event for up to ``durable_block_timeout_s``.
  If the slow subscriber STILL hasn't drained after the timeout we drop
  the oldest item, increment a separate ``durable_drops`` counter, and
  log a warning so operators can spot the slow consumer.
* Legacy 2-arg publish defaults to ``lossy`` behaviour so existing
  string-topic publishers don't surprise-block.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, overload

from gg_relay.core.events import RelayEvent
from gg_relay.core.exceptions import DurableEventDropError
from gg_relay.core.protocol import DurableEventStore

logger = logging.getLogger("gg_relay.bus")

_WILDCARD = "*"


@dataclass(slots=True)
class _Sub:
    """Internal per-subscriber state.

    ``waker`` fires when a new item arrives (or the stream closes);
    ``drained`` fires when the consumer pops at least one item from a
    previously-full deque, letting durable-tier publishers wake up and
    retry the put.
    """

    maxsize: int
    items: deque[Any] = field(default_factory=deque)
    waker: asyncio.Event = field(default_factory=asyncio.Event)
    drained: asyncio.Event = field(default_factory=asyncio.Event)
    closed_flag: bool = False
    dropped: int = 0
    durable_dropped: int = 0


def _topic_for(target: type[RelayEvent] | str) -> str:
    if isinstance(target, str):
        return target
    return target.__name__


class EventBus:
    """Topic-keyed async fan-out with typed + legacy publish APIs.

    Subscribers register a topic (typed class, class-name string, legacy
    string, or wildcard) and receive an async iterator. Subscription is
    cleaned up on iterator close (``aclose`` / GC) or via :meth:`close`.
    """

    def __init__(
        self,
        *,
        durable_block_timeout_s: float = 1.0,
        on_drop: Callable[[str], None] | None = None,
        on_durable_drop: Callable[[str], None] | None = None,
        durable_store: DurableEventStore | None = None,
        strict_durable: bool = False,
    ) -> None:
        self._subs: dict[str, list[_Sub]] = {}
        self._closed = False
        self._durable_block_timeout_s = durable_block_timeout_s
        self._on_drop = on_drop
        self._on_durable_drop = on_durable_drop
        # ── Plan 7 D7.17 (Task 13): durable persistence ──────────────
        # ``durable_store`` plugs the bus into the optional disk tier
        # (SqlAlchemyDurableEventStore in production, InMemory in
        # tests). When set, every ``delivery_tier="durable"`` event is
        # appended to the store BEFORE fan-out so a slow subscriber
        # cannot lose audit data — the SSE Last-Event-ID cursor
        # replays from the store after a disconnect.
        #
        # ``strict_durable`` opts INTO fail-stop behaviour when no
        # store is configured: publishing a durable event raises
        # :class:`DurableEventDropError`. Default ``False`` preserves
        # the Plan 5 backpressure-only semantics (existing tests
        # publish durable events through plain ``EventBus()`` and rely
        # on lossy-on-overflow rather than persist-or-die). Production
        # lifespans set ``durable_store`` so the store presence alone
        # enforces persistence; the flag exists for the
        # publish-without-store unit test.
        self._durable_store = durable_store
        self._strict_durable = strict_durable

    @property
    def dropped_per_topic(self) -> dict[str, int]:
        """Snapshot of per-topic drop counters (summed across subscribers)."""
        return {
            topic: sum(s.dropped for s in subs)
            for topic, subs in self._subs.items()
        }

    @property
    def durable_dropped_per_topic(self) -> dict[str, int]:
        """Snapshot of per-topic *durable* drop counters.

        These are events whose ``delivery_tier == "durable"`` that we
        still had to drop because the slow subscriber didn't drain within
        ``durable_block_timeout_s``. Non-zero values indicate an
        operations problem: investigate the consuming task.
        """
        return {
            topic: sum(s.durable_dropped for s in subs)
            for topic, subs in self._subs.items()
        }

    def subscribe(
        self,
        topic: type[RelayEvent] | str,
        *,
        maxsize: int = 1000,
    ) -> AsyncIterator[Any]:
        """Return an async iterator yielding events delivered to ``topic``.

        ``topic`` accepts:
          * a :class:`RelayEvent` subclass (e.g. ``SessionStateChanged``)
          * its class-name string (e.g. ``"SessionStateChanged"``) — same dispatch
          * any legacy string topic (e.g. ``"frame"`` / ``"hitl"``)
          * ``"*"`` to receive every event regardless of topic
        """
        topic_key = _topic_for(topic)
        sub = _Sub(maxsize=maxsize)
        # Subscribing on a closed bus should not hang the iterator —
        # immediately mark closed so the consumer drains and exits.
        if self._closed:
            sub.closed_flag = True
            sub.waker.set()
        self._subs.setdefault(topic_key, []).append(sub)

        async def _iter() -> AsyncIterator[Any]:
            try:
                while True:
                    while sub.items:
                        was_full = len(sub.items) >= sub.maxsize
                        item = sub.items.popleft()
                        if was_full and not sub.drained.is_set():
                            # Wake any durable publisher awaiting drainage.
                            sub.drained.set()
                        yield item
                    if sub.closed_flag:
                        return
                    sub.waker.clear()
                    await sub.waker.wait()
            finally:
                bucket = self._subs.get(topic_key)
                if bucket is not None and sub in bucket:
                    bucket.remove(sub)

        return _iter()

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
        """Fan out an event to every current subscriber.

        Two call signatures are supported:
          * ``await bus.publish(event_instance)`` — canonical typed form;
            dispatches to subscribers of ``type(event).__name__`` AND the
            wildcard ``"*"``.
          * ``await bus.publish(topic_str, payload)`` — legacy 2-arg form;
            dispatches to ``topic_str`` AND wildcard subscribers.

        Never blocks the publisher (Plan 5 Task 4 layers in a tier-aware
        blocking path for ``delivery_tier="durable"``). Publishing on a
        closed bus is a no-op (logged at debug).
        """
        if self._closed:
            logger.debug("publish on closed bus dropped event")
            return
        if isinstance(topic_or_event, RelayEvent):
            if event is not None:
                raise TypeError(
                    "publish(RelayEvent) takes a single argument; do not pass a second"
                )
            ev = topic_or_event
            topic_key = type(ev).__name__
            tier = ev.delivery_tier
            # ── Plan 7 D7.17: persist durable events BEFORE fan-out ──
            # Persist-first means a subscriber crash after dispatch
            # cannot orphan a durable event — the store is the source
            # of truth for SSE replay. A persist failure (no store in
            # strict mode, or store.persist raised) surfaces as
            # DurableEventDropError so the caller is forced to make a
            # graceful-degradation decision instead of dispatching a
            # half-persisted event.
            if tier == "durable":
                if self._durable_store is not None:
                    try:
                        await self._durable_store.persist(ev)
                    except DurableEventDropError:
                        raise
                    except Exception as exc:
                        raise DurableEventDropError(
                            f"durable_store.persist failed for {topic_key}: {exc}"
                        ) from exc
                elif self._strict_durable:
                    raise DurableEventDropError(
                        f"durable event {topic_key} published but no "
                        "durable_store configured (strict_durable=True)"
                    )
            await self._dispatch(topic_key, ev, tier=tier)
            await self._dispatch(_WILDCARD, ev, tier=tier)
            return
        topic_key = topic_or_event
        # Legacy 2-arg form is lossy by default — no surprise blocking.
        await self._dispatch(topic_key, event, tier="lossy")
        await self._dispatch(_WILDCARD, event, tier="lossy")

    async def _dispatch(
        self, topic_key: str, event: Any, *, tier: str = "lossy"
    ) -> None:
        for sub in self._subs.get(topic_key, ()):
            if len(sub.items) >= sub.maxsize:
                if tier == "durable" and self._durable_block_timeout_s > 0:
                    sub.drained.clear()
                    try:
                        await asyncio.wait_for(
                            sub.drained.wait(),
                            timeout=self._durable_block_timeout_s,
                        )
                    except TimeoutError:
                        sub.durable_dropped += 1
                        logger.warning(
                            "durable event dropped after %.3fs blocking topic=%s",
                            self._durable_block_timeout_s,
                            topic_key,
                        )
                        if self._on_durable_drop is not None:
                            with contextlib.suppress(Exception):
                                self._on_durable_drop(topic_key)
                # Re-check post-await; the consumer may have drained.
                if len(sub.items) >= sub.maxsize:
                    sub.items.popleft()
                    sub.dropped += 1
                    if self._on_drop is not None:
                        with contextlib.suppress(Exception):
                            self._on_drop(topic_key)
            sub.items.append(event)
            sub.waker.set()

    async def replay_after(
        self, *, last_seq: int | None, limit: int = 1000
    ) -> AsyncIterator[RelayEvent]:
        """Yield durable events with seq > ``last_seq`` in order.

        Plan 7 D7.17 — backs the SSE ``Last-Event-ID: "<seq>:<uuid>"``
        cursor. ``last_seq=None`` (header missing or unparseable) or
        ``durable_store=None`` (bus has no disk tier) yields nothing:
        the SSE generator falls through to the live subscriber tail.
        Otherwise we delegate to :meth:`DurableEventStore.fetch_after`
        and yield each row; the SSE renderer is responsible for
        formatting ids as ``"<seq>:<event_id>"`` so the next reconnect
        resumes from the right place.
        """
        if last_seq is None or self._durable_store is None:
            return
        rows = await self._durable_store.fetch_after(
            last_seq=last_seq, limit=limit
        )
        for evt in rows:
            yield evt

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
