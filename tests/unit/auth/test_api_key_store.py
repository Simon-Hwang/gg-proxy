"""ApiKeyStore CRUD — Plan 8 Task 22 / D8.29.

Covers the six store entry points:

  * ``create``                     — happy path + ``ApiKeyConflictError`` on dup label.
  * ``get_by_hash`` / ``get_by_label`` — round-trip after create.
  * ``list``                       — newest-first + ``include_revoked`` toggle.
  * ``revoke``                     — idempotent (returns False on already-revoked).
  * ``touch_last_used``            — populates ``last_used_at``.
  * ``count_active_admins``        — composite index path (admin role + revoked filter).
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from gg_relay.auth.store import ApiKeyStore, hash_key
from gg_relay.core.exceptions import ApiKeyConflictError
from gg_relay.store.engine import create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def store(tmp_path):
    db_file = tmp_path / "keys.db"
    engine = make_async_engine(f"sqlite+aiosqlite:///{db_file}")
    try:
        await create_all_tables(engine)
        yield ApiKeyStore(engine)
    finally:
        await engine.dispose()


async def test_create_then_get_by_hash_round_trip(store: ApiKeyStore) -> None:
    row = await store.create(
        label="alice",
        raw_key="rk_alice",
        role="submitter",
        created_by_label="bootstrap",
        notes="seed",
    )
    assert row["label"] == "alice"
    assert row["role"] == "submitter"
    assert row["key_hash"] == hash_key("rk_alice")
    assert row["revoked_at"] is None

    fetched = await store.get_by_hash(hash_key("rk_alice"))
    assert fetched is not None
    assert fetched["label"] == "alice"
    assert fetched["created_by_label"] == "bootstrap"
    assert fetched["notes"] == "seed"


async def test_create_duplicate_label_raises_conflict(
    store: ApiKeyStore,
) -> None:
    await store.create(label="ci", raw_key="rk_a", role="admin")
    with pytest.raises(ApiKeyConflictError):
        await store.create(label="ci", raw_key="rk_b", role="admin")


async def test_list_filters_revoked_by_default(store: ApiKeyStore) -> None:
    await store.create(label="alive", raw_key="rk_alive", role="viewer")
    await store.create(label="dying", raw_key="rk_dying", role="viewer")
    await store.revoke(label="dying")

    active = await store.list(include_revoked=False)
    labels = {r["label"] for r in active}
    assert labels == {"alive"}

    all_rows = await store.list(include_revoked=True)
    assert {r["label"] for r in all_rows} == {"alive", "dying"}


async def test_revoke_is_idempotent(store: ApiKeyStore) -> None:
    await store.create(label="boom", raw_key="rk_boom", role="admin")
    first = await store.revoke(label="boom")
    second = await store.revoke(label="boom")
    assert first is True
    assert second is False


async def test_touch_last_used_populates_column(
    store: ApiKeyStore,
) -> None:
    await store.create(label="hot", raw_key="rk_hot", role="submitter")
    pre = await store.get_by_label("hot")
    assert pre is not None and pre["last_used_at"] is None

    await store.touch_last_used(key_hash=hash_key("rk_hot"))

    post = await store.get_by_label("hot")
    assert post is not None
    assert post["last_used_at"] is not None


async def test_count_active_admins_excludes_revoked(
    store: ApiKeyStore,
) -> None:
    await store.create(label="root", raw_key="rk_root", role="admin")
    await store.create(label="alt", raw_key="rk_alt", role="admin")
    await store.create(
        label="non-admin", raw_key="rk_na", role="submitter"
    )

    count = await store.count_active_admins()
    assert count == 2

    await store.revoke(label="alt")
    count_after = await store.count_active_admins()
    assert count_after == 1
