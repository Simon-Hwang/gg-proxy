"""DBKeyResolver — Plan 8 Task 22 / D8.29.

Covers the runtime resolver:

  * Happy path (active key → ResolvedKey).
  * Revoked key → None (negative caching).
  * Expired key → None.
  * ``invalidate_cache`` clears both positive and negative entries.
  * ``role_override_mode='config'`` rewrites the role at resolve time.
  * Single-flight: concurrent misses for the same hash share one DB read.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from gg_relay.auth.db_resolver import DBKeyResolver
from gg_relay.auth.store import ApiKeyStore, hash_key
from gg_relay.store.engine import create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def store(tmp_path):
    db_file = tmp_path / "dbres.db"
    engine = make_async_engine(f"sqlite+aiosqlite:///{db_file}")
    try:
        await create_all_tables(engine)
        yield ApiKeyStore(engine)
    finally:
        await engine.dispose()


async def test_resolve_returns_active_key(store: ApiKeyStore) -> None:
    await store.create(label="alice", raw_key="rk_a", role="admin")
    resolver = DBKeyResolver(key_store=store)

    resolved = await resolver.resolve("rk_a")
    assert resolved is not None
    assert resolved.label == "alice"
    assert resolved.role == "admin"


async def test_resolve_revoked_returns_none(store: ApiKeyStore) -> None:
    await store.create(label="bye", raw_key="rk_bye", role="viewer")
    await store.revoke(label="bye")
    resolver = DBKeyResolver(key_store=store)

    assert await resolver.resolve("rk_bye") is None


async def test_resolve_expired_returns_none(store: ApiKeyStore) -> None:
    past = datetime.now(UTC) - timedelta(days=1)
    await store.create(
        label="old", raw_key="rk_old", role="admin", expires_at=past
    )
    resolver = DBKeyResolver(key_store=store)

    assert await resolver.resolve("rk_old") is None


async def test_invalidate_cache_drops_entry(store: ApiKeyStore) -> None:
    await store.create(label="hot", raw_key="rk_hot", role="submitter")
    resolver = DBKeyResolver(key_store=store, cache_ttl=300.0)
    first = await resolver.resolve("rk_hot")
    assert first is not None

    # Revoke directly in the store (bypassing the resolver) — cache
    # still holds the positive entry until we invalidate.
    await store.revoke(label="hot")
    cached = await resolver.resolve("rk_hot")
    assert cached is not None  # stale hit

    await resolver.invalidate_cache(label="hot")
    miss = await resolver.resolve("rk_hot")
    assert miss is None


async def test_role_override_mode_config(store: ApiKeyStore) -> None:
    """role_mapping['alice'] = 'admin' overrides the DB role 'viewer'."""
    await store.create(label="alice", raw_key="rk_a", role="viewer")
    cfg = SimpleNamespace(role_mapping={"alice": "admin"})
    resolver = DBKeyResolver(
        key_store=store, cfg=cfg, role_override_mode="config"
    )

    resolved = await resolver.resolve("rk_a")
    assert resolved is not None
    assert resolved.role == "admin"

    # Default (db) mode passes the DB column through verbatim.
    resolver_default = DBKeyResolver(key_store=store, cfg=cfg)
    resolved_default = await resolver_default.resolve("rk_a")
    assert resolved_default is not None
    assert resolved_default.role == "viewer"


async def test_single_flight_shares_lookup(store: ApiKeyStore) -> None:
    """Concurrent misses for the same key trigger one DB lookup, not many.

    We hook the store's ``get_by_hash`` to count invocations while five
    concurrent ``resolve`` calls race; the single-flight pattern keeps
    the count at 1 even though five coroutines awaited the same hash.
    """
    await store.create(label="solo", raw_key="rk_solo", role="admin")
    resolver = DBKeyResolver(key_store=store)

    calls: list[str] = []
    original = store.get_by_hash

    async def counting(kh: str):
        calls.append(kh)
        # Yield once so concurrent callers all enqueue before the
        # first lookup completes — exercises the inflight Future path.
        await asyncio.sleep(0)
        return await original(kh)

    store.get_by_hash = counting  # type: ignore[method-assign]
    try:
        results = await asyncio.gather(
            *[resolver.resolve("rk_solo") for _ in range(5)]
        )
    finally:
        store.get_by_hash = original  # type: ignore[method-assign]
    assert all(r is not None and r.label == "solo" for r in results)
    assert len(calls) == 1, f"single-flight broken: {len(calls)} DB hits"
    assert calls[0] == hash_key("rk_solo")
