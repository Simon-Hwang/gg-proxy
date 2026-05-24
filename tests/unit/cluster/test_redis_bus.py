"""Plan 9 D9.1 — RedisStreamEventBus tests (fakeredis).

Covers:

1. Round-trip via XADD → XREAD using fakeredis.
2. Topic-keyed subscribers receive only matching events.
3. ``"*"`` wildcard subscriber receives every event.
4. ``subscribe_all(after_seq=None)`` is live-tail (no historical
   replay).
5. ``subscribe_all(after_seq=N)`` replays from a specific cursor.
6. ``close()`` cancels the pump and unblocks subscribers.
7. Decode failures don't crash the pump (logged + skipped).

Why fakeredis (not testcontainers): unit-test layer should be
fast (< 1s/test). The D9.6 integration tests use real Redis via
testcontainers for behaviour that fakeredis can't emulate (real
network, persistence).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest
import pytest_asyncio
from fakeredis import aioredis as fake_aioredis

from gg_relay.cluster import RedisStreamEventBus
from gg_relay.core.events import RelayEvent, SessionCompleted, SessionCreated


@pytest_asyncio.fixture
async def fake_redis():
    """Fresh fakeredis client with decode_responses=True."""
    client = fake_aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def bus(fake_redis):
    """RedisStreamEventBus wired to fakeredis. Auto-closes."""
    bus = RedisStreamEventBus(fake_redis, stream_key="test-events")
    yield bus
    await bus.close()


def _evt(sid: str, *, prompt: str = "hi") -> SessionCreated:
    return SessionCreated(
        session_id=sid,
        occurred_at=datetime.now(UTC),
        prompt_redacted=prompt,
        tags=(),
    )


class TestPublishRoundTrip:
    @pytest.mark.asyncio
    async def test_xadd_writes_to_stream(self, bus, fake_redis) -> None:
        await bus.publish(_evt("s1"))
        # Stream now has 1 entry
        length = await fake_redis.xlen("test-events")
        assert length == 1

    @pytest.mark.asyncio
    async def test_legacy_2arg_form_is_no_op(self, bus, fake_redis) -> None:
        """Legacy ``publish(str, payload)`` was Plan 5 frame fan-out;
        Redis backend drops these silently (local-only by design)."""
        await bus.publish("legacy.topic", {"x": 1})
        length = await fake_redis.xlen("test-events")
        assert length == 0


class TestTopicSubscriber:
    @pytest.mark.asyncio
    async def test_wildcard_subscriber_receives_all(self, bus) -> None:
        sub = bus.subscribe("*")
        # Yield control so the pump task can XREAD $ before publish
        # (otherwise the entries arrive before XREAD starts blocking
        # and are skipped — that's correct production semantics; tests
        # mirror real SSE clients which connect-then-receive).
        await asyncio.sleep(0.1)
        await bus.publish(_evt("s1"))
        await bus.publish(_evt("s2"))
        events: list[RelayEvent] = []
        try:
            async with asyncio.timeout(3.0):
                async for evt in sub:
                    events.append(evt)
                    if len(events) >= 2:
                        break
        except TimeoutError:
            pass
        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_typed_subscriber_filters_by_class(self, bus) -> None:
        sub = bus.subscribe(SessionCreated)
        await asyncio.sleep(0.1)  # let pump start
        await bus.publish(_evt("s1"))
        await bus.publish(
            SessionCompleted(
                session_id="s2",
                occurred_at=datetime.now(UTC),
            )
        )
        events: list[RelayEvent] = []
        try:
            async with asyncio.timeout(2.0):
                async for evt in sub:
                    events.append(evt)
                    break
        except TimeoutError:
            pass
        assert len(events) == 1
        # ReplayedEvent.type_name is the original class name
        assert events[0].type_name == "SessionCreated"  # type: ignore[attr-defined]


class TestSubscribeAll:
    @pytest.mark.asyncio
    async def test_after_seq_none_is_live_tail(self, bus) -> None:
        """``after_seq=None`` reads from $ — historical events are
        NOT replayed. Useful for "start at HEAD" SSE connections."""
        # Pre-populate before subscribing
        await bus.publish(_evt("s1"))
        await bus.publish(_evt("s2"))

        # Live tail should NOT see the two pre-populated events
        all_events: list[RelayEvent] = []

        async def collect():
            async for e in bus.subscribe_all(after_seq=None):
                all_events.append(e)
                if len(all_events) >= 1:
                    break

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0.1)  # ensure XREAD enters block
        await bus.publish(_evt("s3"))
        try:
            async with asyncio.timeout(3.0):
                await collector
        except TimeoutError:
            collector.cancel()
        assert len(all_events) == 1
        assert all_events[0].session_id == "s3"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_after_seq_zero_replays_all_history(self, bus) -> None:
        await bus.publish(_evt("s1"))
        await bus.publish(_evt("s2"))
        await bus.publish(_evt("s3"))
        seen: list[RelayEvent] = []
        async for evt in bus.subscribe_all(after_seq=0, limit=10):
            seen.append(evt)
            if len(seen) >= 3:
                break
        assert len(seen) == 3


class TestClose:
    @pytest.mark.asyncio
    async def test_close_cancels_pump_and_subscribers(
        self, fake_redis
    ) -> None:
        bus = RedisStreamEventBus(fake_redis, stream_key="test-events-c")
        sub = bus.subscribe("*")
        await bus.close()
        # After close, the subscriber's __anext__ should raise
        # StopAsyncIteration within a bounded time.
        events: list[RelayEvent] = []
        try:
            async with asyncio.timeout(3.0):
                async for evt in sub:
                    events.append(evt)
        except TimeoutError:
            pytest.fail("subscriber did not stop after bus.close()")
        # No events were published, so the list stays empty.
        assert events == []


class TestPumpResilience:
    @pytest.mark.asyncio
    async def test_decode_failure_does_not_crash_pump(
        self, bus, fake_redis
    ) -> None:
        """If a stranger XADDs a malformed entry (wrong v field) the
        pump must log + skip, NOT die — otherwise one bad publisher
        wedges every subscriber on every other worker."""
        # Publish a real event first to bring up the pump
        sub = bus.subscribe("*")
        await asyncio.sleep(0.1)  # let pump start
        await bus.publish(_evt("s1"))

        # Now inject malformed entries directly
        await fake_redis.xadd(
            "test-events", {"v": "999", "type": "Garbage"}
        )

        # Then publish a real event again
        await bus.publish(_evt("s2"))

        # Both real events should still arrive at the subscriber
        seen: list[RelayEvent] = []
        try:
            async with asyncio.timeout(3.0):
                async for evt in sub:
                    seen.append(evt)
                    if len(seen) >= 2:
                        break
        except TimeoutError:
            pass
        sids = [
            cast(str, getattr(e, "session_id", "")) for e in seen
        ]
        assert "s1" in sids and "s2" in sids
