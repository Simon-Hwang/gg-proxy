"""Plan 8 Task 14 / D8.24 — dashboard templates page render test."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import bcrypt
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.store import SqlAlchemyStore, create_all_tables, make_async_engine


def _bcrypt_hash(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")


def _make_cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/templates-dash.db"
    cfg.api_keys_raw = ""
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.dashboard_session_secret = SecretStr(
        "test-secret-32-bytes-or-longer-xxxxxxxx"
    )
    cfg.dashboard_users_raw = f"alice={_bcrypt_hash('alice-pw')}"
    cfg.public_base_url = "http://t"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def client_and_store(tmp_path: Path) -> Any:
    cfg = _make_cfg(tmp_path)
    app = create_app(cfg)

    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac, app.router.lifespan_context(app):
        r = await ac.post(
            "/dashboard/login",
            data={"username": "alice", "password": "alice-pw"},
        )
        assert r.status_code == 303, r.text
        yield ac, app.state.store


async def test_templates_page_renders_own_and_shared(
    client_and_store,
) -> None:
    """The dashboard page lists the logged-in user's own template
    PLUS another user's shared template; the private template of
    another user is hidden; the Delete button only renders for the
    rows the current user can mutate (own + admin-flag)."""
    client, store = client_and_store
    assert isinstance(store, SqlAlchemyStore)
    await store.create_template(
        name="alice-own",
        creator="dashboard-alice",
        prompt="alice's own prompt",
        description="own template",
    )
    await store.create_template(
        name="bob-shared",
        creator="bob",
        prompt="bob's shared prompt",
        description="bob shared",
        shared=True,
    )
    await store.create_template(
        name="bob-private",
        creator="bob",
        prompt="bob's secret",
        shared=False,
    )

    r = await client.get("/dashboard/templates")
    assert r.status_code == 200, r.text
    body = r.text
    assert "Prompt Templates" in body
    assert 'id="templates-table"' in body
    # Alice's own template visible.
    assert "alice-own" in body
    # Bob's shared template visible.
    assert "bob-shared" in body
    # Bob's private template NOT visible.
    assert "bob-private" not in body, (
        "private template from another user must be hidden"
    )
    # Delete button renders on alice's own row but not bob's.
    assert 'hx-delete="/api/v1/templates/1"' in body  # alice-own = id 1
    assert 'hx-delete="/api/v1/templates/2"' not in body, (
        "non-creator must not see Delete button for bob-shared"
    )
