"""Plan 8 Task 23 / D8.30 step 5 — per-role default landing view.

Two tests pin the ``/dashboard/`` (no path beyond the prefix) routing
contract:

  * ``test_root_redirects_submitter_to_own_owner`` — a submitter
    landing on the bare ``/dashboard/`` URL gets a 302 to
    ``/dashboard/kanban?owner=dashboard-<self>`` so they see their
    own work first.
  * ``test_root_admin_sees_full_kanban`` — an admin landing on the
    bare URL renders the full kanban template (200) without a
    redirect — admins want the team-wide firehose.

Both tests log in via the legacy admin / new bcrypt user flow and
follow ``follow_redirects=False`` so the 302 surfaces as a direct
response status code rather than being auto-followed to a 200 we
can't easily distinguish from the admin branch.
"""
from __future__ import annotations

import urllib.parse
from pathlib import Path

import bcrypt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.store import create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/role-default.db"
    # Two dashboard users — one submitter, one admin — so a single
    # app instance can exercise both branches without a fresh boot.
    cfg.api_keys_raw = "k1"
    cfg.role_mapping_raw = (
        "dashboard-alice=submitter,dashboard-boss=admin"
    )
    cfg.dashboard_users_raw = (
        f"alice={_hash('a-pw')},boss={_hash('b-pw')}"
    )
    cfg.dashboard_session_secret = SecretStr(
        "test-role-default-secret-32-bytes-min"
    )
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://t"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def client(tmp_path: Path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac, app.router.lifespan_context(app):
        yield ac


async def _login(ac: AsyncClient, username: str, password: str) -> None:
    r = await ac.post(
        "/dashboard/login",
        data={"username": username, "password": password},
    )
    assert r.status_code == 303, r.text


async def test_root_redirects_submitter_to_own_owner(
    client: AsyncClient,
) -> None:
    """Submitter alice hitting ``/dashboard/`` → 302 to
    ``/dashboard/kanban?owner=dashboard-alice``.

    The redirect target carries the URL-quoted ``dashboard-<username>``
    label so the kanban filter form repopulates with the expected
    value. Asserting on ``Location`` (not following the redirect)
    keeps the test focused on the routing contract — a follow
    would silently mask a bug in the kanban filter.
    """
    await _login(client, "alice", "a-pw")
    r = await client.get("/dashboard/")
    assert r.status_code == 302, r.text
    expected_owner = urllib.parse.quote("dashboard-alice")
    assert r.headers["location"] == (
        f"/dashboard/kanban?owner={expected_owner}"
    )


async def test_root_admin_sees_full_kanban(
    client: AsyncClient,
) -> None:
    """Admin boss hitting ``/dashboard/`` → 200 rendering the kanban
    template directly (no redirect).

    The body carries the kanban-specific markup ("Queued" column
    heading) so we know we hit the kanban template and not, say,
    an empty placeholder. A 302 status here would mean the
    role-based redirect mistakenly fired for admins — that's the
    regression this test catches.
    """
    await _login(client, "boss", "b-pw")
    r = await client.get("/dashboard/")
    assert r.status_code == 200, r.text
    body = r.text.lower()
    # Kanban template renders the four lifecycle columns; checking
    # for "queued" is a robust column-heading sentinel that the
    # template was actually rendered (vs. a redirect or 404 body).
    assert "queued" in body
