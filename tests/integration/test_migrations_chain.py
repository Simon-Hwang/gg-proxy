"""Migration chain integrity — Plan 7 Task 6 / D7.5.

Verifies the full ``0001 → 0002 → 0003`` upgrade path plus the
``0003 → 0002`` downgrade roundtrip on SQLite, and (optionally) the
same chain on a real Postgres dialect via ``RELAY_TEST_POSTGRES_URL``
when a Docker daemon is reachable. The test reuses the subprocess
alembic helper from :mod:`test_session_aggregates_migration` rather
than re-implementing it; the indirection avoids re-entering the
running pytest-asyncio loop from inside ``alembic upgrade`` (which
itself calls ``asyncio.run`` in ``store/migrations/env.py``).
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text

from gg_relay.store import make_async_engine

from .test_session_aggregates_migration import (
    _run_alembic,
    _run_downgrade,
    _run_upgrade,
)

pytestmark = pytest.mark.asyncio


# ── SQLite chain (default; runs in every CI job) ────────────────────


@pytest_asyncio.fixture
async def sqlite_db_url(tmp_path: Path):
    """Yield an empty SQLite file URL; caller drives alembic."""
    db_file = tmp_path / "chain.db"
    yield f"sqlite+aiosqlite:///{db_file}"


async def _columns(async_url: str, table: str) -> set[str]:
    engine = make_async_engine(async_url)
    try:
        async with engine.connect() as conn:

            def _inspect(sync_conn):
                return {c["name"] for c in inspect(sync_conn).get_columns(table)}

            return await conn.run_sync(_inspect)
    finally:
        await engine.dispose()


async def test_chain_0001_to_0003_upgrade(sqlite_db_url: str):
    """0001 → 0002 → 0003 顺次 upgrade，最终 sessions/hitl_requests 含新列."""
    _run_upgrade(sqlite_db_url, "head")
    sess_cols = await _columns(sqlite_db_url, "sessions")
    hitl_cols = await _columns(sqlite_db_url, "hitl_requests")
    assert "version" in sess_cols, f"sessions.version missing: {sess_cols}"
    assert "paused_at" in sess_cols, f"sessions.paused_at missing: {sess_cols}"
    assert "version" in hitl_cols, f"hitl_requests.version missing: {hitl_cols}"
    # Sanity: 0002 columns still present (i.e. 0003 didn't accidentally
    # rebuild the table without them).
    for col in {"input_tokens", "output_tokens", "cost_usd", "turn_count"}:
        assert col in sess_cols, f"0002 column {col!r} lost after 0003"


async def test_downgrade_0003_to_0002_roundtrip(sqlite_db_url: str):
    """upgrade head → downgrade -1 → 新列消失，0002 列保留."""
    _run_upgrade(sqlite_db_url, "head")
    _run_downgrade(sqlite_db_url, "0002")
    sess_cols = await _columns(sqlite_db_url, "sessions")
    hitl_cols = await _columns(sqlite_db_url, "hitl_requests")
    for col in {"version", "paused_at"}:
        assert col not in sess_cols, f"sessions.{col} survived downgrade"
    assert "version" not in hitl_cols, "hitl_requests.version survived downgrade"
    # 0002 columns still alive — only 0003 was rolled back.
    assert "input_tokens" in sess_cols
    assert "turn_count" in sess_cols


async def test_existing_rows_get_default_zero(sqlite_db_url: str):
    """0002 状态插一行 session/hitl_request → upgrade 0003 → 已有行 version=0."""
    _run_upgrade(sqlite_db_url, "0002")
    engine = make_async_engine(sqlite_db_url)
    try:
        async with engine.begin() as conn:
            now = datetime.now(UTC).isoformat()
            await conn.execute(
                text(
                    "INSERT INTO sessions "
                    "(id, status, spec_json, tags, submitted_at, backend, "
                    " input_tokens, output_tokens, cost_usd, turn_count) "
                    "VALUES (:id, 'queued', '{}', '[]', :ts, 'inprocess', "
                    " 0, 0, 0, 0)"
                ),
                {"id": "sid-pre", "ts": now},
            )
            await conn.execute(
                text(
                    "INSERT INTO hitl_requests "
                    "(id, session_id, tool, args_json, status, created_at) "
                    "VALUES (:id, :sid, 'Bash', '{}', 'pending', :ts)"
                ),
                {"id": "h-pre", "sid": "sid-pre", "ts": now},
            )
    finally:
        await engine.dispose()
    # Now run 0003 — existing rows must get version=0 via server_default.
    _run_upgrade(sqlite_db_url, "head")
    engine = make_async_engine(sqlite_db_url)
    try:
        async with engine.connect() as conn:
            sess_row = (
                await conn.execute(
                    text(
                        "SELECT version, paused_at FROM sessions "
                        "WHERE id='sid-pre'"
                    )
                )
            ).first()
            hitl_row = (
                await conn.execute(
                    text("SELECT version FROM hitl_requests WHERE id='h-pre'")
                )
            ).first()
        assert sess_row is not None
        assert sess_row[0] == 0, f"sessions.version default not applied: {sess_row[0]!r}"
        assert sess_row[1] is None, f"sessions.paused_at should be NULL: {sess_row[1]!r}"
        assert hitl_row is not None
        assert hitl_row[0] == 0, (
            f"hitl_requests.version default not applied: {hitl_row[0]!r}"
        )
    finally:
        await engine.dispose()


# ── Postgres chain (opt-in via RELAY_TEST_POSTGRES_URL) ─────────────


def _postgres_async_url() -> str | None:
    """Return ``RELAY_TEST_POSTGRES_URL`` normalized to async driver, or None.

    The CI ``requires_docker`` job installs the ``postgres`` extra and
    boots a postgres container; setting ``RELAY_TEST_POSTGRES_URL=
    postgresql://gg:gg@127.0.0.1:5432/gg`` (or the asyncpg variant)
    activates this test. Locally without docker, the env var is unset
    and the test is skipped.
    """
    raw = os.environ.get("RELAY_TEST_POSTGRES_URL")
    if not raw:
        return None
    if raw.startswith("postgresql+asyncpg://"):
        return raw
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw[len("postgresql://") :]
    return raw


@pytest.mark.requires_docker
async def test_chain_on_postgres():
    """Postgres dialect 跑完整 0001→0002→0003 + downgrade roundtrip.

    Runs only when ``RELAY_TEST_POSTGRES_URL`` is set — the CI
    ``requires_docker`` job exports it before invoking pytest. The
    test cleans up by downgrading back to ``base`` so the database
    can be reused across runs.
    """
    url = _postgres_async_url()
    if url is None:
        pytest.skip("RELAY_TEST_POSTGRES_URL not set; skipping Postgres chain test")
    # Start from a clean slate so a previously-failed run doesn't
    # leave tables behind.
    _run_alembic(url, "downgrade", "base")
    try:
        _run_upgrade(url, "head")
        sess_cols = await _columns(url, "sessions")
        hitl_cols = await _columns(url, "hitl_requests")
        assert "version" in sess_cols
        assert "paused_at" in sess_cols
        assert "version" in hitl_cols
        # Downgrade roundtrip — 0003 columns gone, 0002 columns survive.
        _run_downgrade(url, "0002")
        sess_cols = await _columns(url, "sessions")
        hitl_cols = await _columns(url, "hitl_requests")
        assert "version" not in sess_cols
        assert "paused_at" not in sess_cols
        assert "version" not in hitl_cols
        assert "input_tokens" in sess_cols
    finally:
        _run_alembic(url, "downgrade", "base")
