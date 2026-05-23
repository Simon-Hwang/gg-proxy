"""Plan 8 D8.10 — Postgres pool tuning + slow query log (Task 2).

Coverage:

* SQLite engines accept Plan 8 pool kwargs without raising (the kwargs
  are silently dropped because SQLite's pool is effectively
  single-connection in async contexts).
* Postgres URLs forward ``pool_size`` to ``create_async_engine`` and
  the resulting ``QueuePool`` exposes the configured size.
* The slow-query listener emits a ``WARNING`` on
  ``gg_relay.store.engine`` when an executed query exceeds
  ``threshold_ms`` (simulated deterministically by stubbing
  ``time.perf_counter`` on the engine module).
* ``slow_query_log_ms=0`` skips attaching the listener entirely (no
  WARN even when the stubbed clock would otherwise trigger).
* Smoke check against a real Postgres instance (skipped without
  ``RELAY_TEST_POSTGRES_URL``) — confirms the dialect path doesn't
  reject the pool kwargs on a live connection.
"""
from __future__ import annotations

import logging
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.store.engine import make_async_engine


def test_sqlite_engine_accepts_pool_kwargs() -> None:
    """SQLite URLs swallow Plan 8 pool kwargs without raising."""
    engine = make_async_engine(
        "sqlite+aiosqlite:///:memory:",
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=3600,
        slow_query_log_ms=500,
    )
    assert isinstance(engine, AsyncEngine)


def test_postgres_pool_kwargs_applied() -> None:
    """Postgres URLs forward ``pool_size`` to the underlying QueuePool.

    No network connection is attempted; ``create_async_engine`` only
    builds the pool lazily, so the fake hostname is fine.
    """
    pytest.importorskip("asyncpg")
    engine = make_async_engine(
        "postgresql+asyncpg://test:test@nonexistent:5432/test",
        pool_size=20,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=1800,
        slow_query_log_ms=100,
    )
    assert engine.pool.size() == 20


async def test_slow_query_no_log_under_threshold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sub-threshold queries must not produce a ``slow_query`` log."""
    engine = make_async_engine(
        "sqlite+aiosqlite:///:memory:",
        slow_query_log_ms=10_000,  # 10s; ``SELECT 1`` stays well under
    )
    try:
        with caplog.at_level(logging.WARNING, logger="gg_relay.store.engine"):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        slow = [r for r in caplog.records if r.getMessage() == "slow_query"]
        assert not slow, (
            f"unexpected slow_query log under 10s threshold: "
            f"{[r.__dict__ for r in slow]}"
        )
    finally:
        await engine.dispose()


async def test_slow_query_logs_above_threshold(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A simulated slow query triggers a WARN with elapsed/threshold extras.

    Determinism is achieved by stubbing ``perf_counter`` on the engine
    module so every call advances the fake clock by 100 seconds. The
    first call (``before_cursor_execute``) returns 100.0 and the
    second (``after_cursor_execute``) returns 200.0 → elapsed = 100 s,
    well above the 10 ms threshold.
    """
    counter = [0.0]

    def fake_perf() -> float:
        counter[0] += 100.0
        return counter[0]

    monkeypatch.setattr("gg_relay.store.engine.perf_counter", fake_perf)

    # Defensive: earlier CLI tests run ``gg-relay migrate`` which used
    # to invoke ``fileConfig`` with the legacy ``disable_existing_loggers
    # =True`` default, leaving the ``gg_relay.store.engine`` logger
    # marked disabled in-process. env.py now passes ``False`` (Plan 8
    # D8.10 fix); this re-enable is belt-and-braces so a future
    # regression in some sibling fixture can't silently mute caplog.
    target_logger = logging.getLogger("gg_relay.store.engine")
    target_logger.disabled = False

    engine = make_async_engine(
        "sqlite+aiosqlite:///:memory:",
        slow_query_log_ms=10,
    )
    try:
        with caplog.at_level(logging.WARNING, logger="gg_relay.store.engine"):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        slow = [r for r in caplog.records if r.getMessage() == "slow_query"]
        assert slow, (
            "expected slow_query WARN log; captured: "
            f"{[r.getMessage() for r in caplog.records]}"
        )
        rec = slow[0]
        assert rec.levelno == logging.WARNING
        assert rec.name == "gg_relay.store.engine"
        # ``extra`` fields propagate to the LogRecord as attributes.
        assert getattr(rec, "threshold_ms", None) == 10
        assert getattr(rec, "elapsed_ms", 0.0) >= 10.0
        preview = getattr(rec, "statement_preview", "")
        assert "SELECT" in preview.upper()
        assert len(preview) <= 200
    finally:
        await engine.dispose()


async def test_slow_query_disabled_when_threshold_zero(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``slow_query_log_ms=0`` skips attaching the listener entirely.

    The clock stub guarantees that *if* the listener were attached
    every query would log; observing zero slow_query records proves
    the disable path took effect.
    """
    counter = [0.0]

    def fake_perf() -> float:
        counter[0] += 100.0
        return counter[0]

    monkeypatch.setattr("gg_relay.store.engine.perf_counter", fake_perf)

    # See sibling test above — defensive re-enable in case an earlier
    # ``logging.config.fileConfig`` call left this logger disabled.
    logging.getLogger("gg_relay.store.engine").disabled = False

    engine = make_async_engine(
        "sqlite+aiosqlite:///:memory:",
        slow_query_log_ms=0,
    )
    try:
        with caplog.at_level(logging.WARNING, logger="gg_relay.store.engine"):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        slow = [r for r in caplog.records if r.getMessage() == "slow_query"]
        assert not slow, (
            "threshold=0 must not attach the listener; "
            f"got: {[r.getMessage() for r in slow]}"
        )
    finally:
        await engine.dispose()


@pytest.mark.requires_docker
async def test_postgres_pool_pre_ping_smoke() -> None:
    """Real Postgres + pre_ping=True executes ``SELECT 1`` cleanly.

    Opt-in via ``RELAY_TEST_POSTGRES_URL`` (the CI ``requires_docker``
    job exports it). Confirms the dialect doesn't reject Plan 8 pool
    kwargs when the connection actually opens.
    """
    pg_url = os.environ.get("RELAY_TEST_POSTGRES_URL")
    if not pg_url:
        pytest.skip("RELAY_TEST_POSTGRES_URL not set")
    if pg_url.startswith("postgresql://"):
        pg_url = "postgresql+asyncpg://" + pg_url[len("postgresql://"):]

    engine = make_async_engine(
        pg_url,
        pool_size=2,
        max_overflow=1,
        pool_pre_ping=True,
        pool_recycle=300,
        slow_query_log_ms=100,
    )
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            assert result.scalar() == 1
    finally:
        await engine.dispose()
