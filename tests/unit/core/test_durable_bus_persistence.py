"""Durable EventBus persistence + replay — Plan 7 Task 13 (D7.17).

Covers the InMemoryDurableEventStore in isolation (persist returns
monotonic seq, fetch_after replays in seq order with a limit) plus the
end-to-end EventBus integration (durable event without a store +
strict mode raises DurableEventDropError, durable event with a store
gets persisted before fan-out, replay_after yields persisted events in
ascending seq order).
"""
from __future__ import annotations

import pytest

from gg_relay.core import (
    DurableEventDropError,
    EventBus,
    HITLRequested,
    SessionCompleted,
    SessionCreated,
    SessionStateChanged,
)
from gg_relay.store.durable_event import InMemoryDurableEventStore


class TestInMemoryDurableEventStore:
    async def test_persist_returns_monotonic_seq(self) -> None:
        store = InMemoryDurableEventStore()
        s1 = await store.persist(SessionCreated(session_id="a"))
        s2 = await store.persist(
            SessionStateChanged(
                session_id="a", from_state="queued", to_state="running"
            )
        )
        s3 = await store.persist(
            SessionCompleted(session_id="a", status="completed")
        )
        assert (s1, s2, s3) == (1, 2, 3)

    async def test_fetch_after_returns_events_above_cursor(self) -> None:
        store = InMemoryDurableEventStore()
        evs = []
        for idx in range(5):
            ev = SessionCreated(session_id=f"sid-{idx}")
            evs.append(ev)
            await store.persist(ev)
        out = await store.fetch_after(last_seq=2)
        # seq 1,2 are filtered out; remaining are seq 3,4,5 in order.
        assert [type(e).__name__ for e in out] == [
            "SessionCreated",
            "SessionCreated",
            "SessionCreated",
        ]
        assert [e.session_id for e in out] == ["sid-2", "sid-3", "sid-4"]

    async def test_fetch_after_respects_limit(self) -> None:
        store = InMemoryDurableEventStore()
        for idx in range(5):
            await store.persist(SessionCreated(session_id=f"sid-{idx}"))
        out = await store.fetch_after(last_seq=0, limit=2)
        assert len(out) == 2
        assert [e.session_id for e in out] == ["sid-0", "sid-1"]


class TestEventBusDurablePersistence:
    async def test_publish_durable_without_store_in_strict_mode_raises(
        self,
    ) -> None:
        """Strict mode + no store + durable event → fail-stop.

        The strict flag is the explicit opt-in that lets the bus
        refuse to silently drop a durable event when its persistence
        backend isn't wired. Production lifespans wire the SqlA store
        unconditionally; this guard exists for tests / mis-config.
        """
        bus = EventBus(durable_store=None, strict_durable=True)
        try:
            with pytest.raises(DurableEventDropError):
                await bus.publish(SessionCreated(session_id="x"))
        finally:
            await bus.close()

    async def test_publish_durable_with_store_persists_event(self) -> None:
        store = InMemoryDurableEventStore()
        bus = EventBus(durable_store=store)
        try:
            ev = HITLRequested(session_id="s1", req_id="r1", tool="Write")
            await bus.publish(ev)
        finally:
            await bus.close()
        stored = store.stored_events
        assert len(stored) == 1
        assert stored[0] is ev
        # The persist happens BEFORE fan-out: any subscriber that
        # connected after persist still sees the durable event live.

    async def test_replay_after_yields_persisted_events_in_seq_order(
        self,
    ) -> None:
        store = InMemoryDurableEventStore()
        bus = EventBus(durable_store=store)
        try:
            for idx in range(5):
                await bus.publish(SessionCreated(session_id=f"sid-{idx}"))
            replayed: list[str] = []
            async for evt in bus.replay_after(last_seq=2):
                replayed.append(evt.session_id)
        finally:
            await bus.close()
        assert replayed == ["sid-2", "sid-3", "sid-4"]
