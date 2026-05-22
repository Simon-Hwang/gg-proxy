"""EventBus delivery-tier queue policy tests (Plan 5 Task 4 / D5.3).

Verifies:
  * Lossy events on a full queue: drop oldest, increment drop counter.
  * Durable events on a full queue: publisher awaits per-subscriber
    ``drained`` event, then publishes; ``durable_dropped`` stays 0.
  * Durable events whose subscriber never drains within
    ``durable_block_timeout_s``: increment ``durable_dropped`` and fall
    back to drop-oldest.
  * Legacy 2-arg ``publish(topic, payload)`` keeps lossy semantics —
    must not surprise-block existing callers.
"""
from __future__ import annotations

import asyncio

import pytest

from gg_relay.core import EventBus, HITLRequested, SessionOutputChunk


class TestLossyTier:
    async def test_full_queue_drops_oldest_lossy(self):
        bus = EventBus(durable_block_timeout_s=0.0)
        # Lossy by default — SessionOutputChunk has delivery_tier="lossy".
        sub = bus.subscribe(SessionOutputChunk, maxsize=2)
        for i in range(5):
            await bus.publish(SessionOutputChunk(session_id="s", seq=i))
        # Don't consume yet — we want to inspect the deque before close.
        drops = bus.dropped_per_topic.get("SessionOutputChunk", 0)
        # 3 drops: oldest (0, 1, 2) bumped to make room for (3, 4) when
        # maxsize=2. But fan-out also pushes to wildcard if any — we
        # didn't subscribe to wildcard, so only the named topic counter
        # accumulates.
        assert drops == 3
        await bus.close()
        items = [e async for e in sub]
        assert [e.seq for e in items] == [3, 4]
        assert bus.durable_dropped_per_topic.get("SessionOutputChunk", 0) == 0


class TestDurableTier:
    async def test_durable_publisher_blocks_until_drain(self):
        bus = EventBus(durable_block_timeout_s=2.0)
        sub = bus.subscribe(HITLRequested, maxsize=1)
        await bus.publish(
            HITLRequested(session_id="s", req_id="r1", tool="Write")
        )
        # Queue is now full. Schedule a delayed consumer that pops one
        # item; the next publish must wait for that drainage.
        consumed: list[HITLRequested] = []

        async def _consume_one() -> None:
            await asyncio.sleep(0.1)
            # Mutate the internal deque directly so we don't disturb the
            # subscriber's iterator (which would aclose() and unregister
            # the subscriber, defeating the test).
            internal_sub = bus._subs["HITLRequested"][0]  # noqa: SLF001
            consumed.append(internal_sub.items.popleft())
            if not internal_sub.drained.is_set():
                internal_sub.drained.set()

        consumer = asyncio.create_task(_consume_one())
        t0 = asyncio.get_event_loop().time()
        await bus.publish(
            HITLRequested(session_id="s", req_id="r2", tool="Write")
        )
        elapsed = asyncio.get_event_loop().time() - t0
        assert elapsed >= 0.05, f"publish returned suspiciously fast ({elapsed:.3f}s)"
        await consumer
        assert len(consumed) == 1
        assert consumed[0].req_id == "r1"
        # No durable drop — the slow consumer eventually drained in time.
        assert bus.durable_dropped_per_topic.get("HITLRequested", 0) == 0
        await bus.close()
        # r2 was queued after the manual pop — close drains the rest.
        items = [e async for e in sub]
        assert [e.req_id for e in items] == ["r2"]

    async def test_durable_drop_after_timeout(self):
        bus = EventBus(durable_block_timeout_s=0.05)
        sub = bus.subscribe(HITLRequested, maxsize=1)
        # Fill the slot.
        await bus.publish(
            HITLRequested(session_id="s", req_id="r1", tool="Write")
        )
        # Nobody is consuming — the second publish should bail after
        # ``durable_block_timeout_s`` and drop the oldest.
        t0 = asyncio.get_event_loop().time()
        await bus.publish(
            HITLRequested(session_id="s", req_id="r2", tool="Write")
        )
        elapsed = asyncio.get_event_loop().time() - t0
        assert elapsed >= 0.04
        assert bus.durable_dropped_per_topic.get("HITLRequested", 0) == 1
        assert bus.dropped_per_topic.get("HITLRequested", 0) == 1
        await bus.close()
        items = [e async for e in sub]
        assert [e.req_id for e in items] == ["r2"]


class TestLegacyForm:
    async def test_legacy_publish_does_not_block(self):
        bus = EventBus(durable_block_timeout_s=5.0)
        # Subscribe to legacy "frame" topic with very small queue.
        sub = bus.subscribe("frame", maxsize=1)
        await bus.publish("frame", {"seq": 1})
        t0 = asyncio.get_event_loop().time()
        # Even though durable_block_timeout_s is large, legacy str-topic
        # publish must NOT block — otherwise we'd freeze unmigrated
        # callers.
        await bus.publish("frame", {"seq": 2})
        elapsed = asyncio.get_event_loop().time() - t0
        assert elapsed < 0.1
        # Standard lossy drop counter incremented.
        assert bus.dropped_per_topic.get("frame", 0) == 1
        await bus.close()
        items = [e async for e in sub]
        assert items == [{"seq": 2}]


def test_event_bus_init_signature_kwarg_only():
    """``durable_block_timeout_s`` is keyword-only — guards against
    constructors that pass positional ``maxsize``."""
    bus = EventBus(durable_block_timeout_s=0.5)
    assert bus._durable_block_timeout_s == 0.5  # noqa: SLF001
    with pytest.raises(TypeError):
        EventBus(0.5)  # type: ignore[misc]
