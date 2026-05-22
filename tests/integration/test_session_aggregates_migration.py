"""Session aggregates migration + repository tests — Plan 6 Task 8 / D6.12.

Verifies:
1. ``alembic upgrade head`` produces the four new columns + index
2. ``alembic downgrade base`` removes them cleanly
3. ``Repository.update_session_aggregates`` writes the values
4. ``Repository.aggregate_tokens_by_bucket`` returns the expected
   time-bucketed rows on SQLite (the production Postgres path is
   exercised in `test_dashboard_chart.py` via a docker-compose
   fixture in Task 10).
"""
from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text

from gg_relay.store import SessionRepository, make_async_engine

pytestmark = pytest.mark.asyncio


def _run_alembic(db_url: str, *args: str) -> None:
    """Invoke ``alembic`` via subprocess so its embedded ``asyncio.run``
    in env.py doesn't collide with the test's pytest-asyncio loop.

    The URL is passed via ``RELAY_DATABASE_URL`` because env.py reads
    that env var first (see ``store/migrations/env.py``).
    """
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-m", "alembic", *args],
        env={
            "RELAY_DATABASE_URL": db_url,
            "PATH": "/usr/bin:/usr/local/bin",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"alembic {' '.join(args)} failed:\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def _run_upgrade(db_url: str, target: str = "head") -> None:
    _run_alembic(db_url, "upgrade", target)


def _run_downgrade(db_url: str, target: str = "base") -> None:
    _run_alembic(db_url, "downgrade", target)


@pytest_asyncio.fixture
async def migrated_engine(tmp_path: Path):
    db_file = tmp_path / "aggregates.db"
    async_url = f"sqlite+aiosqlite:///{db_file}"
    _run_upgrade(async_url, "head")
    engine = make_async_engine(async_url)
    yield engine, async_url
    await engine.dispose()


class TestMigration:
    async def test_0002_adds_four_columns(self, migrated_engine):
        engine, _ = migrated_engine
        async with engine.connect() as conn:

            def _columns(sync_conn):
                insp = inspect(sync_conn)
                return {c["name"] for c in insp.get_columns("sessions")}

            columns = await conn.run_sync(_columns)
        for col in {"input_tokens", "output_tokens", "cost_usd", "turn_count"}:
            assert col in columns, f"missing column after migration: {col}"

    async def test_0002_creates_completed_at_index(self, migrated_engine):
        engine, _ = migrated_engine
        async with engine.connect() as conn:

            def _indexes(sync_conn):
                insp = inspect(sync_conn)
                return {i["name"] for i in insp.get_indexes("sessions")}

            indexes = await conn.run_sync(_indexes)
        assert "ix_sessions_completed_at" in indexes

    async def test_downgrade_drops_columns(
        self, tmp_path: Path
    ):
        """Roundtrip — upgrade then downgrade and verify the columns
        are gone."""
        db_file = tmp_path / "rt.db"
        async_url = f"sqlite+aiosqlite:///{db_file}"
        _run_upgrade(async_url, "head")
        _run_downgrade(async_url, "0001")  # leaves baseline tables intact
        engine = make_async_engine(async_url)
        try:
            async with engine.connect() as conn:

                def _cols(sync_conn):
                    return {
                        c["name"]
                        for c in inspect(sync_conn).get_columns("sessions")
                    }

                cols = await conn.run_sync(_cols)
            for c in {
                "input_tokens",
                "output_tokens",
                "cost_usd",
                "turn_count",
            }:
                assert c not in cols, f"column {c!r} survived downgrade"
        finally:
            await engine.dispose()


class TestRepositoryAggregates:
    async def test_update_session_aggregates_writes_values(
        self, migrated_engine
    ):
        engine, _ = migrated_engine
        repo = SessionRepository(engine)
        await repo.create_session(
            id="sid-1",
            spec_json={"prompt": "hi"},
            trace_id=None,
            backend="inprocess",
        )
        await repo.update_session_aggregates(
            "sid-1",
            input_tokens=12345,
            output_tokens=6789,
            cost_usd=0.0042,
            turn_count=7,
        )
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT input_tokens, output_tokens, "
                        "cost_usd, turn_count FROM sessions WHERE id='sid-1'"
                    )
                )
            ).first()
        assert row is not None
        assert row[0] == 12345
        assert row[1] == 6789
        assert abs(row[2] - 0.0042) < 1e-9
        assert row[3] == 7

    async def test_aggregate_tokens_by_bucket_sqlite(
        self, migrated_engine
    ):
        engine, _ = migrated_engine
        repo = SessionRepository(engine)
        now = datetime.now(UTC).replace(microsecond=0)
        # Pre-populate three sessions with ended_at spaced 1 minute
        # apart, then aggregate over a 5-minute window with 2-minute
        # buckets — first two sessions fall in bucket 0, third in
        # bucket 1.
        for i, offset_s in enumerate((-300, -200, -120)):
            sid = f"agg-{i}"
            await repo.create_session(
                id=sid,
                spec_json={},
                trace_id=None,
                backend="inprocess",
            )
            ended_at = now + timedelta(seconds=offset_s)
            await repo.update_session_status(
                sid,
                status="completed",
                ended_at=ended_at,
            )
            await repo.update_session_aggregates(
                sid,
                input_tokens=100 * (i + 1),
                output_tokens=50 * (i + 1),
                cost_usd=0.01 * (i + 1),
                turn_count=i + 1,
            )

        rows = await repo.aggregate_tokens_by_bucket(
            window_s=600, bucket_s=120, now=now
        )
        # All three sessions fall inside the 600s window.
        assert len(rows) >= 1
        # Sum across the returned buckets equals the total injected.
        total_in = sum(r["input_tokens"] for r in rows)
        total_out = sum(r["output_tokens"] for r in rows)
        total_cost = sum(r["cost_usd"] for r in rows)
        assert total_in == 100 + 200 + 300
        assert total_out == 50 + 100 + 150
        assert abs(total_cost - (0.01 + 0.02 + 0.03)) < 1e-9
        # Buckets are sorted ascending.
        bucket_starts = [r["bucket_start"] for r in rows]
        assert bucket_starts == sorted(bucket_starts)
