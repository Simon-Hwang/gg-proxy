"""Plan 7 D7.5 / Task 8 — optimistic-locking smoke on real Postgres.

Mirrors the SQLite-backed unit tests in
:mod:`tests.unit.store.test_optimistic_locking` against the Postgres
dialect so we catch any dialect-specific UPDATE-rowcount quirks before
production. Opt-in via the ``RELAY_TEST_POSTGRES_URL`` env var (the CI
``requires_docker`` job exports it after booting the Postgres
container); locally without the env var the tests are skipped.

Two checks (one per affected table):

* :func:`test_session_concurrency_on_postgres` — the
  ``update_session_status(expected_version=...)`` path raises
  :class:`ConcurrencyError` on a stale read.
* :func:`test_hitl_concurrency_on_postgres` — the
  ``upsert_hitl(expected_version=...)`` path likewise raises
  :class:`ConcurrencyError` without any retry.

Each test cleans its tables on entry so reruns are idempotent. Tables
are created via :func:`create_all_tables` (not Alembic) because the
focus here is the row-level UPDATE behaviour, not migrations — the
migration chain has its own dedicated Postgres test in
:mod:`tests.integration.test_migrations_chain`.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.store import (
    ConcurrencyError,
    SqlAlchemyStore,
    create_all_tables,
    make_async_engine,
)
from gg_relay.store.schema import hitl_requests, metadata, sessions

pytestmark = pytest.mark.asyncio


def _postgres_async_url() -> str | None:
    """Return the async-dialect Postgres URL from env, or None to skip."""
    raw = os.environ.get("RELAY_TEST_POSTGRES_URL")
    if not raw:
        return None
    if raw.startswith("postgresql+asyncpg://"):
        return raw
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw[len("postgresql://"):]
    return raw


@pytest_asyncio.fixture
async def pg_engine() -> AsyncEngine:
    """Yield an :class:`AsyncEngine` bound to the test Postgres instance.

    Skips when ``RELAY_TEST_POSTGRES_URL`` is unset (local dev without
    docker). Drops + re-creates every metadata table so each test
    starts from a clean slate even if a previous run left rows.
    """
    url = _postgres_async_url()
    if url is None:
        pytest.skip("RELAY_TEST_POSTGRES_URL not set; skipping Postgres tests")
    eng = make_async_engine(url)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.drop_all)
    await create_all_tables(eng)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(metadata.drop_all)
    await eng.dispose()


@pytest.mark.requires_docker
async def test_session_concurrency_on_postgres(pg_engine: AsyncEngine) -> None:
    """Postgres dialect raises ConcurrencyError on a stale version read."""
    store = SqlAlchemyStore(pg_engine)
    await store.create_session(
        id="pg-race", spec_json={}, trace_id=None, backend="inprocess"
    )
    assert await store.get_session_version("pg-race") == 0

    # Winner bumps 0 → 1.
    new_v = await store.update_session_status(
        "pg-race", status="running", expected_version=0
    )
    assert new_v == 1

    # Loser reuses v=0; Postgres reports rowcount=0 → ConcurrencyError.
    with pytest.raises(ConcurrencyError) as exc_info:
        await store.update_session_status(
            "pg-race", status="paused", expected_version=0
        )
    assert exc_info.value.expected_version == 0
    assert exc_info.value.actual_version == 1

    # State unchanged by the failed second write.
    row = await store.get_session("pg-race")
    assert row is not None
    assert row["status"] == "running"
    assert row["version"] == 1
    # Defensive: schema tables are accessible (sanity that we're on
    # the right metadata).
    assert sessions.c.id is not None


@pytest.mark.requires_docker
async def test_hitl_concurrency_on_postgres(pg_engine: AsyncEngine) -> None:
    """Postgres dialect raises ConcurrencyError on a stale HITL upsert."""
    store = SqlAlchemyStore(pg_engine)
    await store.create_session(
        id="pg-hitl-race", spec_json={}, trace_id=None, backend="inprocess"
    )
    await store.upsert_hitl(
        id="pg-hitl-race:r1",
        session_id="pg-hitl-race",
        tool="Bash",
        args_json={"cmd": "ls"},
        status="pending",
    )
    assert await store.get_hitl_version("pg-hitl-race:r1") == 0

    # First resolver wins.
    new_v = await store.upsert_hitl(
        id="pg-hitl-race:r1",
        session_id="pg-hitl-race",
        tool="Bash",
        args_json={"cmd": "ls"},
        status="accepted",
        resolved_at=datetime.now(UTC),
        resolver="admin-A",
        expected_version=0,
    )
    assert new_v == 1

    # Second resolver loses immediately — HITL is the 0-retry path.
    with pytest.raises(ConcurrencyError) as exc_info:
        await store.upsert_hitl(
            id="pg-hitl-race:r1",
            session_id="pg-hitl-race",
            tool="Bash",
            args_json={"cmd": "ls"},
            status="denied",
            resolved_at=datetime.now(UTC),
            resolver="admin-B",
            expected_version=0,
        )
    assert exc_info.value.expected_version == 0
    assert exc_info.value.actual_version == 1
    # Winning decision survives the failed second write.
    row = await store.get_hitl("pg-hitl-race:r1")
    assert row is not None
    assert row["status"] == "accepted"
    assert row["resolver"] == "admin-A"
    # Defensive sanity: hitl_requests schema is loaded.
    assert hitl_requests.c.id is not None
