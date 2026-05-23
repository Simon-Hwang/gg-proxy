"""Plan 8 Task 3 / D8.26 — dashboard bcrypt login flow integration tests.

Exercises the upgraded ``/dashboard/login`` route against a real
FastAPI app + SessionMiddleware + the dashboard router:

* GET ``/dashboard/login`` renders the form (HTML).
* POST ``/dashboard/login`` with a username present in
  :attr:`Config.dashboard_users` and a matching bcrypt-checked
  password → 303 redirect to ``/dashboard/sessions`` + cookie set.
* POST ``/dashboard/login`` with a wrong password → 401 + the
  ``invalid credentials`` message rendered in HTML.

The fixture sets ``cfg.dashboard_users_raw`` so the @property
:attr:`Config.dashboard_users` returns ``{"alice": "<bcrypt>"}``;
the legacy admin path stays available too but is not exercised here
(the original ``test_dashboard.py`` already covers it).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import bcrypt
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.executor.protocol import ExecutorBackend
from gg_relay.session.frames import make_msg_chunk, make_session_end
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.spec import SessionSpec
from gg_relay.session.transport.protocol import SessionTransport


async def _trivial_runner(transport: SessionTransport, spec: SessionSpec) -> None:
    del spec
    await transport.send(make_msg_chunk(1, {"text": "hello"}))
    await transport.send(make_session_end(2, "completed", tokens={}, cost_usd=0.0))


def _factory() -> Any:
    def _build(
        kind: str,
        policy: ToolPolicy,
        coordinator: HITLCoordinator,
        session_id: str,
        **kwargs: object,
    ) -> ExecutorBackend:
        del kind, policy, coordinator, session_id, kwargs
        return InProcessExecutor(runner=_trivial_runner)

    return _build


def _bcrypt_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _cfg_with_dashboard_users(tmp_path: Path) -> Config:
    """Build a Config with ``alice`` registered in ``dashboard_users``
    via the env-shaped raw CSV. Uses a deliberately weak bcrypt cost
    (the default of 12 is fine for production but slows the test
    suite by ~250ms per hash; we accept the default here because the
    fixture only mints two hashes per test run)."""
    alice_hash = _bcrypt_hash("alice-pw")
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/login.db"
    cfg.api_keys_raw = "k1"
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.dashboard_admin_password = SecretStr("hunter2")
    cfg.dashboard_session_secret = SecretStr("a-test-secret-32-bytes-or-longer-xxxx")
    cfg.dashboard_users_raw = f"alice={alice_hash}"
    cfg.public_base_url = "http://t"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def client(tmp_path: Path):
    cfg = _cfg_with_dashboard_users(tmp_path)
    app = create_app(cfg)
    app.state.executor_factory_override = _factory()
    from gg_relay.store import create_all_tables, make_async_engine

    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac, app.router.lifespan_context(app):
        yield ac


async def test_login_form_get_returns_html(client: AsyncClient) -> None:
    """GET /dashboard/login → 200 + HTML form including the
    ``Sign in`` button so the operator's browser can render it
    without further JS."""
    r = await client.get("/dashboard/login")
    assert r.status_code == 200
    body = r.text
    assert "<form" in body
    assert 'name="username"' in body
    assert 'name="password"' in body
    assert "Sign in" in body


async def test_login_submit_valid_creds(client: AsyncClient) -> None:
    """POST with the configured bcrypt-verified password → 303 redirect
    to /dashboard/sessions and the session cookie set. The subsequent
    GET /dashboard/sessions (with the same cookie jar) MUST land on
    200, proving the SESSION_USER_KEY entry survives the round-trip."""
    r = await client.post(
        "/dashboard/login",
        data={"username": "alice", "password": "alice-pw"},
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/dashboard/sessions"
    # Cookie was set; the protected page now resolves.
    r2 = await client.get("/dashboard/sessions")
    assert r2.status_code == 200


async def test_login_submit_invalid_creds(client: AsyncClient) -> None:
    """Wrong password → 401 with the ``invalid credentials`` error
    rendered into the login template; no session cookie issued so a
    subsequent /dashboard/sessions request still redirects."""
    r = await client.post(
        "/dashboard/login",
        data={"username": "alice", "password": "WRONG"},
    )
    assert r.status_code == 401
    assert "invalid credentials" in r.text.lower()
    # Still anonymous — protected page redirects to /dashboard/login.
    r2 = await client.get("/dashboard/sessions")
    assert r2.status_code == 303
    assert r2.headers["location"] == "/dashboard/login"
