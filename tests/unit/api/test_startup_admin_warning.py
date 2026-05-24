"""Lifespan no-admin warning — Plan 8 Task 20 / D8.28.

After the env→DB sync + dashboard internal-key refresh, the lifespan
asks ``ApiKeyStore.count_active_admins()`` and emits a one-shot
``logger.warning`` plus sets ``app.state.warn_no_admin = True`` when
the result is 0. This guards against a fresh deployment exposing
``/api/v1/admin/*`` with nobody authorised to call those endpoints.

Two assertions:

* ``warn_no_admin = True`` when no admin role is mapped — the env
  sync still creates a row for the API key, but the role mapping
  resolves to the default ``"submitter"`` so the admin count is 0.
* ``warn_no_admin = False`` when an admin role is mapped via
  ``role_mapping_raw`` — the env sync attaches ``role="admin"`` and
  the count is 1.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.store import create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


def _make_cfg(tmp_path: Path, *, api_keys_raw: str, role_mapping_raw: str) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/admin-warn.db"
    cfg.api_keys_raw = api_keys_raw
    cfg.role_mapping_raw = role_mapping_raw
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://localhost:8000"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


async def _boot_app(cfg: Config):
    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    app = create_app(cfg)
    lifespan_ctx = app.router.lifespan_context(app)
    await lifespan_ctx.__aenter__()
    return app, lifespan_ctx


async def test_lifespan_warns_when_no_admin(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _make_cfg(
        tmp_path,
        api_keys_raw="seed-key:seed",
        role_mapping_raw="seed=submitter",
    )

    with caplog.at_level(logging.WARNING, logger="gg_relay.api"):
        app, lifespan_ctx = await _boot_app(cfg)
        try:
            assert getattr(app.state, "warn_no_admin", None) is True
        finally:
            await lifespan_ctx.__aexit__(None, None, None)

    warned = [
        rec
        for rec in caplog.records
        if rec.name == "gg_relay.api"
        and rec.levelno == logging.WARNING
        and "NO ACTIVE ADMIN API KEY" in rec.getMessage()
    ]
    assert warned, (
        "expected a lifespan WARNING about missing admin, got "
        f"{[r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]}"
    )


async def test_lifespan_silent_when_admin_present(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _make_cfg(
        tmp_path,
        api_keys_raw="admin-key:admin",
        role_mapping_raw="admin=admin",
    )

    with caplog.at_level(logging.WARNING, logger="gg_relay.api"):
        app, lifespan_ctx = await _boot_app(cfg)
        try:
            assert getattr(app.state, "warn_no_admin", None) is False
        finally:
            await lifespan_ctx.__aexit__(None, None, None)

    bad = [
        rec
        for rec in caplog.records
        if rec.name == "gg_relay.api"
        and rec.levelno == logging.WARNING
        and "NO ACTIVE ADMIN API KEY" in rec.getMessage()
    ]
    assert not bad, (
        "did not expect the no-admin WARNING when an admin is mapped; got "
        f"{[r.getMessage() for r in bad]}"
    )
