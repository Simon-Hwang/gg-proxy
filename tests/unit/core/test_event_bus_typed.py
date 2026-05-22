"""EventBus typed-publish + str-compat tests (Plan 5 D5.2=A3).

Companion to ``test_event_bus.py`` which covers the legacy 2-arg form and
backpressure semantics. This module focuses on:

* one-arg ``publish(RelayEvent)`` with class-name topic routing
* dual-form ``subscribe(EventClass)`` vs ``subscribe("EventClass")``
* wildcard ``subscribe("*")`` receives every event
* subscriber cleanup on iterator exit / bus close
* multi-subscriber broadcast
"""
from __future__ import annotations

import asyncio

import pytest

from gg_relay.core import (
    EventBus,
    SessionCompleted,
    SessionCreated,
    SessionOutputChunk,
    SessionStateChanged,
)


class TestTypedPublish:
    async def test_publish_event_routes_by_class_name(self):
        bus = EventBus()
        sub = bus.subscribe(SessionCreated)
        ev = SessionCreated(session_id="s1", prompt_redacted="hi")
        await bus.publish(ev)
        await bus.close()
        items = [e async for e in sub]
        assert items == [ev]

    async def test_subscribe_by_string_classname_equivalent_to_type(self):
        bus = EventBus()
        type_sub = bus.subscribe(SessionCreated)
        str_sub = bus.subscribe("SessionCreated")
        ev = SessionCreated(session_id="s1")
        await bus.publish(ev)
        await bus.close()
        assert [e async for e in type_sub] == [ev]
        assert [e async for e in str_sub] == [ev]

    async def test_wildcard_receives_all_typed_events(self):
        bus = EventBus()
        wild = bus.subscribe("*")
        await bus.publish(SessionCreated(session_id="a"))
        await bus.publish(
            SessionStateChanged(
                session_id="a", from_state="queued", to_state="running"
            )
        )
        await bus.publish(
            SessionCompleted(session_id="a", status="completed")
        )
        await bus.close()
        items = [e async for e in wild]
        assert [type(e).__name__ for e in items] == [
            "SessionCreated",
            "SessionStateChanged",
            "SessionCompleted",
        ]

    async def test_subscriber_isolation_between_event_types(self):
        bus = EventBus()
        created_sub = bus.subscribe(SessionCreated)
        chunk_sub = bus.subscribe(SessionOutputChunk)
        await bus.publish(SessionCreated(session_id="x"))
        await bus.publish(SessionOutputChunk(session_id="x", seq=1))
        await bus.close()
        created_items = [e async for e in created_sub]
        chunk_items = [e async for e in chunk_sub]
        assert len(created_items) == 1
        assert len(chunk_items) == 1
        assert isinstance(created_items[0], SessionCreated)
        assert isinstance(chunk_items[0], SessionOutputChunk)


class TestLegacyCompat:
    """The 2-arg form keeps working until every subscriber migrates."""

    async def test_legacy_publish_subscribe_still_works(self):
        bus = EventBus()
        sub = bus.subscribe("frame")
        await bus.publish("frame", {"type": "msg.chunk", "seq": 1})
        await bus.close()
        items = [e async for e in sub]
        assert items == [{"type": "msg.chunk", "seq": 1}]

    async def test_legacy_publish_also_hits_wildcard(self):
        bus = EventBus()
        wild = bus.subscribe("*")
        await bus.publish("frame", {"type": "msg.chunk"})
        await bus.close()
        items = [e async for e in wild]
        assert items == [{"type": "msg.chunk"}]


class TestPublishContract:
    async def test_passing_second_arg_with_typed_event_raises(self):
        bus = EventBus()
        with pytest.raises(TypeError, match="single argument"):
            await bus.publish(  # type: ignore[call-overload]
                SessionCreated(session_id="x"), "extra"
            )

    async def test_publish_after_close_is_noop(self):
        bus = EventBus()
        sub = bus.subscribe(SessionCreated)
        await bus.close()
        await bus.publish(SessionCreated(session_id="x"))
        items = [e async for e in sub]
        assert items == []


class TestMultiSubscriber:
    async def test_multiple_subscribers_all_receive(self):
        bus = EventBus()
        a = bus.subscribe(SessionStateChanged)
        b = bus.subscribe(SessionStateChanged)
        wild = bus.subscribe("*")
        ev = SessionStateChanged(
            session_id="s", from_state="queued", to_state="running"
        )
        await bus.publish(ev)
        await bus.close()
        assert [e async for e in a] == [ev]
        assert [e async for e in b] == [ev]
        assert [e async for e in wild] == [ev]


class TestSubscriberCleanup:
    async def test_iterator_exit_removes_subscriber(self):
        bus = EventBus()
        sub = bus.subscribe(SessionCreated)
        await bus.publish(SessionCreated(session_id="x"))

        async def consume():
            async for e in sub:
                assert isinstance(e, SessionCreated)
                break

        await consume()
        await sub.aclose()  # type: ignore[attr-defined]
        # Bus has no subscribers left for the topic.
        assert bus._subs.get("SessionCreated", []) == []  # type: ignore[attr-defined]


class TestConcurrentPublishers:
    async def test_concurrent_typed_publishers(self):
        bus = EventBus()
        sub = bus.subscribe(SessionOutputChunk, maxsize=200)

        async def producer(start: int) -> None:
            for i in range(start, start + 25):
                await bus.publish(SessionOutputChunk(session_id="s", seq=i))
                await asyncio.sleep(0)

        await asyncio.gather(producer(0), producer(100))
        await bus.close()
        seqs = sorted(e.seq for e in [evt async for evt in sub])
        assert seqs == sorted(list(range(0, 25)) + list(range(100, 125)))
