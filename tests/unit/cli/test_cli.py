"""CLI command coverage — typer CliRunner."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gg_relay.cli import _parse_duration, app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch) -> str:
    db = tmp_path / "test.db"
    url = f"sqlite+aiosqlite:///{db}"
    monkeypatch.setenv("RELAY_DATABASE_URL", url)
    monkeypatch.setenv("RELAY_API_KEYS_RAW", "test-key")
    monkeypatch.setenv("RELAY_PUBLIC_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("RELAY_DASHBOARD_ADMIN_PASSWORD", "x")
    monkeypatch.setenv("RELAY_DASHBOARD_SESSION_SECRET", "y")
    return url


class TestParseDuration:
    def test_days(self):
        assert _parse_duration("30d") == timedelta(days=30)

    def test_hours(self):
        assert _parse_duration("12h") == timedelta(hours=12)

    def test_minutes(self):
        assert _parse_duration("5m") == timedelta(minutes=5)

    def test_seconds(self):
        assert _parse_duration("60s") == timedelta(seconds=60)

    def test_invalid_raises(self):
        import typer

        with pytest.raises(typer.BadParameter):
            _parse_duration("bogus")


class TestCheckSecrets:
    def test_ok_when_all_present(self, runner: CliRunner, monkeypatch):
        monkeypatch.setenv("RELAY_API_KEYS_RAW", "k1,k2")
        monkeypatch.setenv("RELAY_PUBLIC_BASE_URL", "http://x")
        monkeypatch.setenv("RELAY_DASHBOARD_ADMIN_PASSWORD", "p")
        monkeypatch.setenv("RELAY_DASHBOARD_SESSION_SECRET", "s")
        # Use --mix-stderr=False to capture stderr separately
        result = runner.invoke(app, ["check-secrets"])
        assert result.exit_code == 0, result.output
        assert "OK" in result.output

    def test_fails_with_missing(
        self, runner: CliRunner, monkeypatch, tmp_path: Path
    ):
        # Hermetic: chdir into an empty tmp dir so pydantic-settings'
        # ``env_file=".env"`` discovery doesn't pick up the developer's
        # local repo-root .env file and silently re-supply the secrets
        # we're about to delete from the process env.
        monkeypatch.chdir(tmp_path)
        for k in (
            "RELAY_API_KEYS_RAW",
            "RELAY_PUBLIC_BASE_URL",
            "RELAY_DASHBOARD_ADMIN_PASSWORD",
            "RELAY_DASHBOARD_SESSION_SECRET",
        ):
            monkeypatch.delenv(k, raising=False)
        result = runner.invoke(app, ["check-secrets"])
        assert result.exit_code == 1
        assert "missing" in result.output.lower()


class TestMigrate:
    def test_migrate_creates_tables(
        self, runner: CliRunner, temp_db: str, tmp_path: Path, monkeypatch
    ):
        # Run from repo root so alembic.ini is found
        monkeypatch.chdir(Path(__file__).resolve().parents[3])
        result = runner.invoke(app, ["migrate"])
        assert result.exit_code == 0, result.stdout + result.stderr
        # Verify the tables exist on disk
        import sqlite3

        db_path = temp_db.split("///", 1)[1]
        with sqlite3.connect(db_path) as c:
            names = {
                r[0]
                for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert {"sessions", "frames", "hitl_requests"}.issubset(names)


class TestPrune:
    def test_dry_run(
        self, runner: CliRunner, temp_db: str, monkeypatch
    ):
        result = runner.invoke(app, ["prune", "--older-than", "1d", "--dry-run"])
        assert result.exit_code == 0
        assert "dry-run" in result.stdout

    def test_deletes_old_frames(
        self, runner: CliRunner, temp_db: str, tmp_path: Path
    ):
        # Create tables and insert one old + one new frame
        import asyncio

        from gg_relay.store import (
            SessionRepository,
            create_all_tables,
            make_async_engine,
        )

        async def _setup():
            eng = make_async_engine(temp_db)
            await create_all_tables(eng)
            store = SessionRepository(eng)
            await store.create_session(
                id="x", spec_json={}, trace_id=None, backend="inprocess"
            )
            old = datetime.now(UTC) - timedelta(days=10)
            new = datetime.now(UTC)
            await store.append_frame(
                "x", seq=1, ts=old, type_="msg.chunk", payload={}
            )
            await store.append_frame(
                "x", seq=2, ts=new, type_="msg.chunk", payload={}
            )
            await eng.dispose()

        asyncio.run(_setup())
        result = runner.invoke(app, ["prune", "--older-than", "5d"])
        assert result.exit_code == 0
        assert "deleted 1" in result.stdout


class TestRecover:
    def test_recover_emits_count(
        self, runner: CliRunner, temp_db: str
    ):
        import asyncio

        from gg_relay.store import (
            SessionRepository,
            create_all_tables,
            make_async_engine,
        )

        async def _setup():
            eng = make_async_engine(temp_db)
            await create_all_tables(eng)
            store = SessionRepository(eng)
            await store.create_session(
                id="a", spec_json={}, trace_id=None, backend="inprocess"
            )
            await store.update_session_status("a", status="running")
            await eng.dispose()

        asyncio.run(_setup())
        result = runner.invoke(app, ["recover"])
        assert result.exit_code == 0
        assert "marked 1" in result.stdout


class TestVersion:
    def test_version_prints_something(self, runner: CliRunner):
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert result.stdout.strip()
