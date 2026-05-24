"""EnvKeyResolver bootstrap — Plan 8 Task 22 / D8.29.

Verifies the idempotent sync semantics:

  * Fresh DB → all env keys land with roles from role_mapping.
  * Re-run on an already-synced DB → no new rows (skipped).
  * env key whose hash differs from the DB row for the same label →
    warning emitted, DB takes precedence.
"""
from __future__ import annotations

import logging

import pytest
import pytest_asyncio

from gg_relay.auth.env_resolver import EnvKeyResolver
from gg_relay.auth.store import ApiKeyStore, hash_key
from gg_relay.store.engine import create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def store(tmp_path):
    db_file = tmp_path / "env.db"
    engine = make_async_engine(f"sqlite+aiosqlite:///{db_file}")
    try:
        await create_all_tables(engine)
        yield ApiKeyStore(engine)
    finally:
        await engine.dispose()


async def test_fresh_sync_creates_all_keys(store: ApiKeyStore) -> None:
    resolver = EnvKeyResolver(
        env_keys_with_labels={"rk1": "alice", "rk2": "bob"},
        role_mapping={"alice": "admin", "bob": "submitter"},
        key_store=store,
    )
    summary = await resolver.sync_to_db()
    assert summary == {"created": 2, "skipped": 0}

    alice = await store.get_by_label("alice")
    bob = await store.get_by_label("bob")
    assert alice is not None and alice["role"] == "admin"
    assert bob is not None and bob["role"] == "submitter"
    assert alice["created_by_label"] == "env_bootstrap"


async def test_resync_is_idempotent(store: ApiKeyStore) -> None:
    resolver = EnvKeyResolver(
        env_keys_with_labels={"rk1": "alice"},
        role_mapping={"alice": "admin"},
        key_store=store,
    )
    first = await resolver.sync_to_db()
    second = await resolver.sync_to_db()
    assert first == {"created": 1, "skipped": 0}
    assert second == {"created": 0, "skipped": 1}


async def test_diverging_env_key_warns_and_skips(
    store: ApiKeyStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Pre-populate DB with the OLD key hash.
    await store.create(
        label="alice", raw_key="OLD", role="admin",
        created_by_label="env_bootstrap",
    )
    resolver = EnvKeyResolver(
        env_keys_with_labels={"NEW": "alice"},
        role_mapping={"alice": "admin"},
        key_store=store,
    )
    with caplog.at_level(logging.WARNING, logger="gg_relay.auth.env_resolver"):
        summary = await resolver.sync_to_db()
    assert summary == {"created": 0, "skipped": 1}
    # DB row still carries the OLD hash, not the new env value.
    row = await store.get_by_label("alice")
    assert row is not None
    assert row["key_hash"] == hash_key("OLD")
    assert any(
        "differs from DB row" in rec.message for rec in caplog.records
    )
