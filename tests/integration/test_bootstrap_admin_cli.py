"""``gg-relay bootstrap-admin`` CLI — Plan 8 Task 22 / D8.29.

Two tests:

  * Happy path — creates an admin row in the DB and prints the raw
    key + label.
  * Duplicate label — second invocation with the same label exits
    non-zero (``ApiKeyConflictError`` → typer.Exit(1)).

The CLI itself wraps work in ``asyncio.run`` so the test functions
are intentionally sync (typer's :class:`CliRunner` cannot be invoked
from inside a pytest-asyncio loop without nested-loop errors).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gg_relay.auth.store import ApiKeyStore
from gg_relay.cli import app as cli_app
from gg_relay.store.engine import create_all_tables, make_async_engine


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch) -> str:
    """Create a fresh SQLite DB + schema bootstrap; export env."""
    db = tmp_path / "bootstrap.db"
    url = f"sqlite+aiosqlite:///{db}"

    async def _init() -> None:
        engine = make_async_engine(url)
        try:
            await create_all_tables(engine)
        finally:
            await engine.dispose()

    asyncio.run(_init())
    monkeypatch.setenv("RELAY_DATABASE_URL", url)
    monkeypatch.setenv("RELAY_API_KEYS_RAW", "ignored")
    monkeypatch.setenv("RELAY_PUBLIC_BASE_URL", "http://x")
    monkeypatch.setenv("RELAY_DASHBOARD_ADMIN_PASSWORD", "p")
    monkeypatch.setenv("RELAY_DASHBOARD_SESSION_SECRET", "s")
    return url


def test_bootstrap_admin_creates_db_row(temp_db: str) -> None:
    runner = CliRunner()
    result = runner.invoke(cli_app, ["bootstrap-admin", "--label", "root"])
    assert result.exit_code == 0, result.output
    assert "Raw key:" in result.output
    assert "root" in result.output

    async def _check() -> None:
        engine = make_async_engine(temp_db)
        try:
            store = ApiKeyStore(engine)
            row = await store.get_by_label("root")
            assert row is not None
            assert row["role"] == "admin"
            assert row["created_by_label"] == "bootstrap-admin-cli"
        finally:
            await engine.dispose()

    asyncio.run(_check())


def test_bootstrap_admin_duplicate_label_exits_nonzero(
    temp_db: str,
) -> None:
    runner = CliRunner()
    first = runner.invoke(cli_app, ["bootstrap-admin", "--label", "dup"])
    assert first.exit_code == 0, first.output

    second = runner.invoke(cli_app, ["bootstrap-admin", "--label", "dup"])
    assert second.exit_code == 1
