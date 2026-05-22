"""HTMX dashboard integration tests.

Covers login (good + bad creds), authz on protected pages, sessions list
HTML structure, session detail HTML structure, 404 for unknown session,
and the HTMX HITL approve roundtrip.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

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
    ) -> ExecutorBackend:
        del kind, policy, coordinator, session_id
        return InProcessExecutor(runner=_trivial_runner)

    return _build


def _cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/dash.db"
    cfg.api_keys_raw = "k1"
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.dashboard_admin_password = SecretStr("hunter2")
    cfg.dashboard_session_secret = SecretStr("a-test-secret-32-bytes-or-longer-xxxx")
    cfg.public_base_url = "http://t"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def client(tmp_path: Path):
    cfg = _cfg(tmp_path)
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


async def _login(ac: AsyncClient, password: str = "hunter2") -> None:
    r = await ac.post(
        "/dashboard/login",
        data={"username": "admin", "password": password},
    )
    assert r.status_code == 303, r.text


# ── auth ───────────────────────────────────────────────────────────────


class TestAuth:
    async def test_login_page_renders(self, client: AsyncClient):
        r = await client.get("/dashboard/login")
        assert r.status_code == 200
        assert "Sign in" in r.text

    async def test_login_wrong_password_401(self, client: AsyncClient):
        r = await client.post(
            "/dashboard/login",
            data={"username": "admin", "password": "wrong"},
        )
        assert r.status_code == 401
        assert "invalid" in r.text.lower()

    async def test_login_success_redirects_to_sessions(
        self, client: AsyncClient
    ):
        r = await client.post(
            "/dashboard/login",
            data={"username": "admin", "password": "hunter2"},
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/dashboard/sessions"

    async def test_protected_page_redirects_when_anonymous(
        self, client: AsyncClient
    ):
        r = await client.get("/dashboard/sessions")
        assert r.status_code == 303
        assert r.headers["location"] == "/dashboard/login"

    async def test_logout_clears_session(self, client: AsyncClient):
        await _login(client)
        r1 = await client.get("/dashboard/sessions")
        assert r1.status_code == 200
        r2 = await client.post("/dashboard/logout")
        assert r2.status_code == 303
        r3 = await client.get("/dashboard/sessions")
        assert r3.status_code == 303


# ── pages ──────────────────────────────────────────────────────────────


class TestPages:
    async def test_sessions_list_html_structure(self, client: AsyncClient):
        await _login(client)
        r = await client.get("/dashboard/sessions")
        assert r.status_code == 200
        assert "<table" in r.text
        assert "Sessions" in r.text
        assert "hx-trigger" in r.text

    async def test_session_detail_after_submit(
        self, client: AsyncClient, tmp_path: Path
    ):
        await _login(client)
        body = {
            "spec": {
                "prompt": "hello",
                "cwd": str(tmp_path),
                "plugins": {"profile": "minimal"},
                "executor": "inprocess",
                "timeout_s": 5,
            },
            "credentials": {"ANTHROPIC_API_KEY": "sk-ant-leak"},
        }
        r = await client.post(
            "/api/v1/sessions", json=body, headers={"X-API-Key": "k1"}
        )
        assert r.status_code == 202
        sid = r.json()["id"]
        await asyncio.sleep(0.3)
        r2 = await client.get(f"/dashboard/sessions/{sid}")
        assert r2.status_code == 200
        # No credential leak in rendered HTML.
        assert "sk-ant-leak" not in r2.text
        assert sid in r2.text

    async def test_detail_404(self, client: AsyncClient):
        await _login(client)
        r = await client.get("/dashboard/sessions/nope")
        assert r.status_code == 404


# ── HITL via HTMX form ─────────────────────────────────────────────────


class TestHITLForm:
    async def test_resolve_unknown_returns_409(self, client: AsyncClient):
        await _login(client)
        r = await client.post(
            "/dashboard/sessions/sX/hitl/rX",
            data={"decision": "accept"},
        )
        assert r.status_code == 409

    async def test_resolve_invalid_decision_400(self, client: AsyncClient):
        await _login(client)
        r = await client.post(
            "/dashboard/sessions/sX/hitl/rX",
            data={"decision": "maybe"},
        )
        assert r.status_code == 400
