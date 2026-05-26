"""Lifespan schema-drift fail-fast warning — Plan v3 hardening.

Pins the regression net for the very issue the user reported:
``no such table: user_credentials`` after a code update because
``gg-relay migrate`` was forgotten. The lifespan does NOT
auto-upgrade (operator-controlled by design) but it DOES emit a
loud WARN so the operator notices BEFORE traffic hits.

Two tests:

  * stale alembic_version (e.g. 0012 against a head=0013 codebase)
    → WARN emitted with the actionable "run gg-relay migrate" hint.
  * matching alembic_version → no warning (silent happy path).

Test path opt-out (no alembic_version row at all → silent skip)
is covered implicitly by EVERY other integration test that uses
``create_all_tables`` instead of alembic.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.store import create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


def _cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/drift.db"
    cfg.api_keys_raw = "k1"
    cfg.dashboard_admin_password = SecretStr("hunter2")
    cfg.dashboard_session_secret = SecretStr(
        "schema-drift-test-secret-32-bytes-min"
    )
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://t"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def fresh_db(tmp_path: Path):
    cfg = _cfg(tmp_path)
    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    yield cfg, eng
    await eng.dispose()


async def _seed_alembic_version(eng, version: str) -> None:
    """Forge an ``alembic_version`` row so the lifespan check has
    something to compare against."""
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
        )
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
            {"v": version},
        )


async def _boot_and_drain(cfg: Config) -> None:
    app = create_app(cfg)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://t", follow_redirects=False
    ) as ac, app.router.lifespan_context(app):
        # One health probe so the test exercises a real round-trip,
        # not just the lifespan body.
        r = await ac.get("/healthz")
        assert r.status_code in (200, 503), r.text


async def test_stale_alembic_head_emits_actionable_warning(
    fresh_db, caplog
):
    """0012 in DB vs 0013 in code → WARN with the actionable hint."""
    cfg, eng = fresh_db
    await _seed_alembic_version(eng, "0012")
    with caplog.at_level(logging.WARNING):
        await _boot_and_drain(cfg)
    drift = [
        r for r in caplog.records
        if "DB schema drift" in r.getMessage()
    ]
    assert drift, (
        "lifespan must emit a WARN when alembic_version is behind "
        "the code's expected head; otherwise operators only learn "
        "via per-route 500 (the exact bug this test guards)"
    )
    msg = drift[0].getMessage()
    assert "0012" in msg, "WARN must name the DB version"
    assert "gg-relay migrate" in msg, "WARN must include the fix command"


async def test_matching_alembic_head_is_silent(fresh_db, caplog):
    """When DB head matches code head no drift WARN is emitted —
    the check must not be noisy in healthy deployments."""
    cfg, eng = fresh_db
    # Seed with current head (read it the same way the lifespan does).
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(
        AlembicConfig("alembic.ini")
    ).get_current_head()
    await _seed_alembic_version(eng, head or "0013")
    with caplog.at_level(logging.WARNING):
        await _boot_and_drain(cfg)
    drift = [
        r for r in caplog.records
        if "DB schema drift" in r.getMessage()
    ]
    assert not drift, (
        f"healthy deployment should not see a drift WARN; got: "
        f"{[r.getMessage() for r in drift]!r}"
    )
