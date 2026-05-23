"""Plan 7 Task 15 (D7.22) — ``/readyz`` DB + draining check tests.

Validates the three gates implemented in
:mod:`gg_relay.api.routers.health`:

  * happy path → 200 ``{"status": "ready"}``
  * engine raises on ``SELECT 1`` → 503 ``db_unreachable``
  * SessionManager not accepting → 503 ``manager_draining``

``/healthz`` stays as a pure liveness probe and must remain 200 even
when readiness is failing (verified inline so a regression that wires
the DB ping into ``/healthz`` is caught here).
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

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
    await transport.send(make_msg_chunk(1, {"x": 1}))
    await transport.send(make_session_end(2, "completed", tokens={}, cost_usd=0.0))


def _factory_override() -> Callable[..., ExecutorBackend]:
    def _factory(
        kind: str,
        policy: ToolPolicy,
        coordinator: HITLCoordinator,
        session_id: str,
        **kwargs: object,
    ) -> ExecutorBackend:
        del kind, policy, coordinator, session_id, kwargs
        return InProcessExecutor(runner=_trivial_runner)

    return _factory


def _make_cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/health.db"
    cfg.api_keys_raw = "k1"
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://localhost:8000"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def app_and_client(tmp_path: Path) -> Any:
    cfg = _make_cfg(tmp_path)
    app = create_app(cfg)
    app.state.executor_factory_override = _factory_override()
    from gg_relay.store import create_all_tables, make_async_engine

    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as ac, app.router.lifespan_context(app):
        yield app, ac


class TestReadyzGates:
    async def test_readyz_ok_when_db_and_manager_healthy(self, app_and_client):
        _app, client = app_and_client
        r = await client.get("/readyz")
        assert r.status_code == 200
        assert r.json() == {"status": "ready"}

    async def test_readyz_503_when_db_unreachable(self, app_and_client):
        app, client = app_and_client

        class _BadEngine:
            def connect(self):
                raise RuntimeError("simulated dbnotreachable")

        original = app.state.engine
        app.state.engine = _BadEngine()
        try:
            r = await client.get("/readyz")
        finally:
            app.state.engine = original
        assert r.status_code == 503
        # ``HTTPException(detail=...)`` lands in ``detail`` field of the body.
        body = r.json()
        assert "db_unreachable" in body["detail"]
        assert "RuntimeError" in body["detail"]
        # Liveness probe is unaffected by readiness-only failures.
        r_live = await client.get("/healthz")
        assert r_live.status_code == 200

    async def test_readyz_503_when_manager_draining(self, app_and_client):
        app, client = app_and_client
        # Simulate the post-shutdown drain state by flipping the flag.
        # The router checks ``manager.accepting_new``; the contract token
        # in the response body is ``"manager_draining"``.
        app.state.manager._accepting_new = False
        r = await client.get("/readyz")
        assert r.status_code == 503
        assert r.json()["detail"] == "manager_draining"
        # /healthz remains green so k8s only drains traffic, doesn't restart.
        r_live = await client.get("/healthz")
        assert r_live.status_code == 200
