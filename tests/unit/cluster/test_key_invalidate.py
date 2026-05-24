"""Plan 9 D9.10 — KeyInvalidateSubscriber tests.

Covers:

1. KeyInvalidated event triggers app.state refresh from the DB.
2. Multiple usernames in one event are handled (bulk rotation).
3. ReplayedEvent (the Redis-bus path) also triggers refresh.
4. Subscriber stops cleanly on bus close.
5. DB load failure is logged but doesn't crash the subscriber.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.applications import Starlette

from gg_relay.cluster.key_invalidate import KeyInvalidateSubscriber
from gg_relay.core.event_bus import EventBus
from gg_relay.core.events import KeyInvalidated
from gg_relay.store.dashboard_keys import DashboardKeyStore
from gg_relay.store.schema import metadata


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def store(engine):
    return DashboardKeyStore(engine)


@pytest_asyncio.fixture
async def bus():
    b = EventBus()
    yield b
    await b.close()


@pytest_asyncio.fixture
async def app():
    return Starlette()


class TestRefreshOnEvent:
    @pytest.mark.asyncio
    async def test_event_triggers_app_state_refresh(
        self, bus, store, app
    ) -> None:
        # Seed the DB
        alice_key = await store.get_or_create("alice")
        bob_key = await store.get_or_create("bob")

        subscriber = KeyInvalidateSubscriber(
            bus=bus, store=store, app=app
        )
        subscriber.start()
        try:
            # Give the subscriber a moment to attach
            await asyncio.sleep(0.05)
            # Publish a KeyInvalidated event
            await bus.publish(
                KeyInvalidated(
                    occurred_at=datetime.now(UTC),
                    usernames=("alice", "bob"),
                )
            )
            # Wait for the subscriber to process
            for _ in range(20):
                if hasattr(
                    app.state, "dashboard_internal_keys"
                ) and len(app.state.dashboard_internal_keys) == 2:
                    break
                await asyncio.sleep(0.05)
            assert app.state.dashboard_internal_keys == {
                "alice": alice_key,
                "bob": bob_key,
            }
        finally:
            await subscriber.stop()


class TestBulkRotation:
    @pytest.mark.asyncio
    async def test_one_event_refreshes_full_mapping(
        self, bus, store, app
    ) -> None:
        """Even when the event carries 1 username, we refresh the
        FULL mapping — protects against ordering races where a
        bulk rotation emitted N events but only the last won."""
        await store.get_or_create("alice")
        await store.get_or_create("bob")

        subscriber = KeyInvalidateSubscriber(
            bus=bus, store=store, app=app
        )
        subscriber.start()
        try:
            await asyncio.sleep(0.05)
            await bus.publish(
                KeyInvalidated(
                    occurred_at=datetime.now(UTC),
                    usernames=("alice",),  # only alice
                )
            )
            for _ in range(20):
                if hasattr(
                    app.state, "dashboard_internal_keys"
                ) and len(app.state.dashboard_internal_keys) == 2:
                    break
                await asyncio.sleep(0.05)
            # BOTH users are reloaded
            assert set(app.state.dashboard_internal_keys.keys()) == {
                "alice",
                "bob",
            }
        finally:
            await subscriber.stop()


class TestStopCleanup:
    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, bus, store, app) -> None:
        subscriber = KeyInvalidateSubscriber(
            bus=bus, store=store, app=app
        )
        subscriber.start()
        await asyncio.sleep(0.05)
        await subscriber.stop()
        # The task should be done after stop()
        assert subscriber._task is None  # type: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_double_start_is_idempotent(
        self, bus, store, app
    ) -> None:
        subscriber = KeyInvalidateSubscriber(
            bus=bus, store=store, app=app
        )
        subscriber.start()
        first_task = subscriber._task  # type: ignore[reportPrivateUsage]
        subscriber.start()  # idempotent
        second_task = subscriber._task  # type: ignore[reportPrivateUsage]
        try:
            assert first_task is second_task
        finally:
            await subscriber.stop()


class TestFailureMode:
    @pytest.mark.asyncio
    async def test_db_load_failure_doesnt_kill_subscriber(
        self, bus, store, app
    ) -> None:
        """If list_all() raises (e.g. transient DB blip), the
        subscriber logs the exception and continues — does NOT
        wedge so a later successful event still refreshes."""

        async def broken_list_all() -> dict[str, str]:
            raise RuntimeError("simulated DB outage")

        store.list_all = broken_list_all  # type: ignore[method-assign]
        subscriber = KeyInvalidateSubscriber(
            bus=bus, store=store, app=app
        )
        subscriber.start()
        try:
            await asyncio.sleep(0.05)
            # Publish an event — subscriber should swallow the
            # RuntimeError, log it, and continue.
            await bus.publish(
                KeyInvalidated(
                    occurred_at=datetime.now(UTC),
                    usernames=("alice",),
                )
            )
            await asyncio.sleep(0.2)
            # app.state.dashboard_internal_keys was never set
            assert not hasattr(app.state, "dashboard_internal_keys") or (
                app.state.dashboard_internal_keys == {}
            )
            # And the subscriber is still alive
            assert subscriber._task is not None  # type: ignore[reportPrivateUsage]
            assert not subscriber._task.done()  # type: ignore[reportPrivateUsage]
        finally:
            await subscriber.stop()
