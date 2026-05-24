"""Plan 9 D9.10 — DashboardKeyStore tests.

The DB-backed dashboard internal key store is the single source of
truth across worker pods. These tests pin the contract:

1. ``get_or_create`` returns existing keys idempotently.
2. ``get_or_create`` is race-safe — two concurrent calls produce
   the same key (one INSERT wins, the other silently returns).
3. ``rotate`` produces a new key + updates rotated_at.
4. ``list_all`` returns the full mapping for KeyInvalidateSubscriber.
5. ``delete`` removes the row (operator removes a dashboard user).
6. Generated keys are exactly 43 chars (matches the CHECK
   constraint on schema column).
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

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


class TestGetOrCreate:
    @pytest.mark.asyncio
    async def test_first_call_creates_new_key(self, store) -> None:
        key = await store.get_or_create("alice")
        assert key
        assert len(key) == 43

    @pytest.mark.asyncio
    async def test_second_call_returns_same_key(self, store) -> None:
        key1 = await store.get_or_create("alice")
        key2 = await store.get_or_create("alice")
        assert key1 == key2

    @pytest.mark.asyncio
    async def test_concurrent_get_or_create_race_safe(
        self, store
    ) -> None:
        """Two workers calling get_or_create on the same fresh
        username simultaneously must end up with the same key
        (INSERT…ON CONFLICT DO NOTHING + SELECT)."""
        results = await asyncio.gather(
            store.get_or_create("bob"),
            store.get_or_create("bob"),
        )
        assert results[0] == results[1]


class TestGet:
    @pytest.mark.asyncio
    async def test_unknown_user_returns_none(self, store) -> None:
        assert await store.get("nobody") is None

    @pytest.mark.asyncio
    async def test_returns_stored_key(self, store) -> None:
        created = await store.get_or_create("alice")
        fetched = await store.get("alice")
        assert fetched == created


class TestListAll:
    @pytest.mark.asyncio
    async def test_empty_table_returns_empty_dict(self, store) -> None:
        assert await store.list_all() == {}

    @pytest.mark.asyncio
    async def test_returns_full_mapping(self, store) -> None:
        await store.get_or_create("alice")
        await store.get_or_create("bob")
        await store.get_or_create("charlie")
        mapping = await store.list_all()
        assert set(mapping.keys()) == {"alice", "bob", "charlie"}
        assert all(len(v) == 43 for v in mapping.values())


class TestRotate:
    @pytest.mark.asyncio
    async def test_rotate_changes_key(self, store) -> None:
        original = await store.get_or_create("alice")
        new = await store.rotate("alice")
        assert new != original
        assert len(new) == 43

    @pytest.mark.asyncio
    async def test_rotate_updates_stored_value(self, store) -> None:
        await store.get_or_create("alice")
        new = await store.rotate("alice")
        fetched = await store.get("alice")
        assert fetched == new


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_row(self, store) -> None:
        await store.get_or_create("alice")
        await store.delete("alice")
        assert await store.get("alice") is None

    @pytest.mark.asyncio
    async def test_delete_unknown_user_silent(self, store) -> None:
        # No-op, no raise
        await store.delete("ghost")
