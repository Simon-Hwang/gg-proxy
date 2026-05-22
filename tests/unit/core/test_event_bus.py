"""EventBus pub/sub semantics."""
from __future__ import annotations

import asyncio

import pytest

from gg_relay.core import EventBus


class TestPubSub:
    async def test_single_subscriber_receives_published(self):
        bus = EventBus()
        sub = bus.subscribe("frame")
        await bus.publish("frame", {"seq": 1})
        await bus.close()
        items = [evt async for evt in sub]
        assert items == [{"seq": 1}]

    async def test_multi_subscriber_broadcast(self):
        bus = EventBus()
        a = bus.subscribe("frame")
        b = bus.subscribe("frame")
        await bus.publish("frame", {"seq": 1})
        await bus.publish("frame", {"seq": 2})
        await bus.close()
        items_a = [evt async for evt in a]
        items_b = [evt async for evt in b]
        assert items_a == [{"seq": 1}, {"seq": 2}]
        assert items_b == [{"seq": 1}, {"seq": 2}]

    async def test_topic_isolation(self):
        bus = EventBus()
        frame_sub = bus.subscribe("frame")
        hitl_sub = bus.subscribe("hitl")
        await bus.publish("frame", {"x": 1})
        await bus.publish("hitl", {"y": 2})
        await bus.close()
        assert [evt async for evt in frame_sub] == [{"x": 1}]
        assert [evt async for evt in hitl_sub] == [{"y": 2}]

    async def test_subscriber_cleanup_on_close(self):
        bus = EventBus()
        sub = bus.subscribe("t")
        await bus.close()
        # iterator should exit cleanly
        items = [e async for e in sub]
        assert items == []

    async def test_publish_after_close_is_noop(self):
        bus = EventBus()
        sub = bus.subscribe("t")
        await bus.close()
        await bus.publish("t", {"x": 1})  # no exception, no delivery
        assert [e async for e in sub] == []


class TestBackpressure:
    async def test_full_queue_drops_oldest(self):
        bus = EventBus()
        sub = bus.subscribe("t", maxsize=2)
        await bus.publish("t", "a")
        await bus.publish("t", "b")
        await bus.publish("t", "c")  # oldest "a" should drop
        # Sample drop counter BEFORE iterating so the bus still tracks the
        # subscriber (iterator exit removes it from the bus registry).
        assert bus.dropped_per_topic == {"t": 1}
        await bus.close()
        items = [e async for e in sub]
        assert items == ["b", "c"]

    async def test_multiple_drops_counted(self):
        bus = EventBus()
        sub = bus.subscribe("t", maxsize=1)
        for i in range(5):
            await bus.publish("t", i)
        assert bus.dropped_per_topic == {"t": 4}
        await bus.close()
        items = [e async for e in sub]
        assert items == [4]

    async def test_close_is_idempotent(self):
        bus = EventBus()
        await bus.close()
        await bus.close()  # second call is no-op

    async def test_subscriber_aclose_cleans_up(self):
        """Explicitly aclosing the iterator removes it from the bus."""
        bus = EventBus()
        sub = bus.subscribe("t")
        await bus.publish("t", 1)

        async def consume():
            async for e in sub:
                assert e == 1
                break

        await consume()
        # Force the async generator's finally to run before we assert.
        await sub.aclose()  # type: ignore[attr-defined]
        assert bus._subs.get("t", []) == []  # type: ignore[attr-defined]


class TestSessionStateEnum:
    def test_enum_values_match_strings(self):
        from gg_relay.core import SessionState

        assert SessionState.QUEUED == "queued"
        assert SessionState.RUNNING == "running"
        assert SessionState.COMPLETED == "completed"
        assert SessionState.FAILED == "failed"
        assert SessionState.CANCELLED == "cancelled"
        assert SessionState.INTERRUPTED == "interrupted"

    def test_terminal_states_set(self):
        from gg_relay.core import TERMINAL_STATES, SessionState

        assert SessionState.QUEUED not in TERMINAL_STATES
        assert SessionState.RUNNING not in TERMINAL_STATES
        assert SessionState.COMPLETED in TERMINAL_STATES
        assert SessionState.FAILED in TERMINAL_STATES
        assert SessionState.CANCELLED in TERMINAL_STATES
        assert SessionState.INTERRUPTED in TERMINAL_STATES

    def test_session_summary_dataclass(self):
        from datetime import UTC, datetime

        from gg_relay.core import SessionState, SessionSummary

        s = SessionSummary(
            id="s",
            status=SessionState.QUEUED,
            submitted_at=datetime.now(UTC),
            started_at=None,
            ended_at=None,
            tags=("x",),
        )
        assert s.tags == ("x",)
        # Frozen — assignment raises FrozenInstanceError (subclass of AttributeError).
        with pytest.raises(AttributeError):
            s.id = "other"  # type: ignore[misc]


class TestSubscriberAfterPublishOrdering:
    async def test_subscriber_must_register_before_publish(self):
        """Subscribers registered after publish miss earlier events."""
        bus = EventBus()
        await bus.publish("t", "lost")
        sub = bus.subscribe("t")
        await bus.publish("t", "kept")
        await bus.close()
        assert [e async for e in sub] == ["kept"]

    async def test_subscribe_iterator_yields_in_publish_order(self):
        bus = EventBus()
        sub = bus.subscribe("t")
        for i in range(50):
            await bus.publish("t", i)
        await bus.close()
        items = [e async for e in sub]
        assert items == list(range(50))


class TestAsyncDriving:
    async def test_concurrent_publishers(self):
        bus = EventBus()
        sub = bus.subscribe("t", maxsize=200)

        async def producer(start: int) -> None:
            for i in range(start, start + 50):
                await bus.publish("t", i)
                await asyncio.sleep(0)

        await asyncio.gather(producer(0), producer(100))
        await bus.close()
        items = sorted([e async for e in sub])
        assert items == sorted(list(range(0, 50)) + list(range(100, 150)))
