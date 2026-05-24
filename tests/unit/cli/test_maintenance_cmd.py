"""``gg-relay maintenance`` CLI — Plan 8 Task 20 / D8.3.

Two black-box tests via ``typer.testing.CliRunner``:

* ``--dry-run`` exits 0, prints the per-table summary, and leaves
  the database untouched.
* a live run actually deletes old rows.

Both tests pre-create the schema on a tmp SQLite file and seed the
``events`` table with two rows (one ancient, one recent) so the
preview / live distinction is observable from stdout.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gg_relay.cli import app
from gg_relay.store.engine import create_all_tables, make_async_engine
from gg_relay.store.schema import events


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch) -> str:
    db_file = tmp_path / "maint.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    monkeypatch.setenv("RELAY_DATABASE_URL", url)
    monkeypatch.setenv("RELAY_API_KEYS_RAW", "test-key:alice")
    monkeypatch.setenv("RELAY_PUBLIC_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("RELAY_DASHBOARD_ADMIN_PASSWORD", "x")
    monkeypatch.setenv("RELAY_DASHBOARD_SESSION_SECRET", "y")

    async def _seed() -> None:
        eng = make_async_engine(url)
        try:
            await create_all_tables(eng)
            now = datetime.now(timezone.utc)
            old = now - timedelta(days=120)
            async with eng.begin() as conn:
                await conn.execute(
                    events.insert().values(
                        event_id="e-old",
                        ts=old,
                        type="TestEvent",
                        session_id=None,
                        payload={},
                        delivery_tier="disk",
                    )
                )
                await conn.execute(
                    events.insert().values(
                        event_id="e-new",
                        ts=now,
                        type="TestEvent",
                        session_id=None,
                        payload={},
                        delivery_tier="disk",
                    )
                )
        finally:
            await eng.dispose()

    asyncio.run(_seed())
    return url


def _count_events(url: str) -> int:
    async def _do() -> int:
        eng = make_async_engine(url)
        try:
            async with eng.connect() as conn:
                rows = (await conn.execute(events.select())).all()
                return len(rows)
        finally:
            await eng.dispose()

    return asyncio.run(_do())


def test_maintenance_dry_run_prints_summary_no_delete(
    runner: CliRunner, seeded_db: str
) -> None:
    result = runner.invoke(app, ["maintenance", "--dry-run"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    out = result.stdout
    assert "DRY RUN" in out
    assert "events" in out
    assert "audit_log" in out
    assert "hitl_requests" in out
    assert "Would delete" in out
    assert _count_events(seeded_db) == 2


def test_maintenance_live_run_deletes_old_rows(
    runner: CliRunner, seeded_db: str
) -> None:
    result = runner.invoke(app, ["maintenance"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    out = result.stdout
    assert "LIVE" in out
    assert "Deleted" in out
    assert _count_events(seeded_db) == 1
