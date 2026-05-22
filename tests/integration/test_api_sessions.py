"""FastAPI /api/v1/sessions integration tests.

Each test gets a fresh sqlite tempfile + a fresh app instance so the
SessionManager / EventBus state is fully isolated.
"""
from __future__ import annotations

import asyncio
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
    ) -> ExecutorBackend:
        del kind, policy, coordinator, session_id
        return InProcessExecutor(runner=_trivial_runner)

    return _factory


def _make_cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    # Mutate via __dict__ since BaseSettings is not frozen but each call to
    # cfg.field = x would re-validate; pydantic accepts direct assignment for
    # str/Path fields without issue.
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/api.db"
    cfg.api_keys_raw = "k1,k2"
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://localhost:8000"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncClient:
    cfg = _make_cfg(tmp_path)
    app = create_app(cfg)
    app.state.executor_factory_override = _factory_override()
    # Ensure tables exist before lifespan boots SessionManager.
    from gg_relay.store import create_all_tables, make_async_engine

    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as ac, app.router.lifespan_context(app):
        yield ac


HEADERS = {"X-API-Key": "k1"}


def _spec_body(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    body = {
        "spec": {
            "prompt": "hello",
            "cwd": str(tmp_path),
            "plugins": {"profile": "minimal"},
            "executor": "inprocess",
            "timeout_s": 5,
            "tags": [],
        },
        "credentials": {},
    }
    body["spec"].update(overrides)
    return body


# ── auth ───────────────────────────────────────────────────────────────


class TestAuth:
    async def test_missing_api_key_rejected(
        self, client: AsyncClient, tmp_path: Path
    ):
        r = await client.post("/api/v1/sessions", json=_spec_body(tmp_path))
        assert r.status_code == 401

    async def test_wrong_api_key_rejected(
        self, client: AsyncClient, tmp_path: Path
    ):
        r = await client.post(
            "/api/v1/sessions",
            json=_spec_body(tmp_path),
            headers={"X-API-Key": "wrong"},
        )
        assert r.status_code == 401

    async def test_accepts_any_configured_key(
        self, client: AsyncClient, tmp_path: Path
    ):
        # k2 was also in api_keys_raw
        r = await client.post(
            "/api/v1/sessions",
            json=_spec_body(tmp_path),
            headers={"X-API-Key": "k2"},
        )
        assert r.status_code == 202

    async def test_health_unauthenticated(self, client: AsyncClient):
        r = await client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    async def test_readyz_reports_ready(self, client: AsyncClient):
        r = await client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"


# ── submit ─────────────────────────────────────────────────────────────


class TestSubmit:
    async def test_submit_returns_session_id(
        self, client: AsyncClient, tmp_path: Path
    ):
        r = await client.post(
            "/api/v1/sessions", json=_spec_body(tmp_path), headers=HEADERS
        )
        assert r.status_code == 202
        body = r.json()
        assert "id" in body
        assert body["status"] in {"queued", "running", "completed"}
        assert "credentials" not in body
        assert "credentials" not in str(body)

    async def test_submit_with_credentials_redacts(
        self, client: AsyncClient, tmp_path: Path
    ):
        body = _spec_body(tmp_path)
        body["credentials"] = {"ANTHROPIC_API_KEY": "sk-ant-leaktest"}
        r = await client.post(
            "/api/v1/sessions", json=body, headers=HEADERS
        )
        assert r.status_code == 202
        full = r.text
        assert "sk-ant-leaktest" not in full
        assert "ANTHROPIC_API_KEY" not in full

    async def test_invalid_body_rejected(
        self, client: AsyncClient, tmp_path: Path
    ):
        r = await client.post(
            "/api/v1/sessions",
            json={"foo": "bar"},
            headers=HEADERS,
        )
        assert r.status_code == 422


# ── list + get ─────────────────────────────────────────────────────────


class TestListAndGet:
    async def test_list_empty(self, client: AsyncClient):
        r = await client.get("/api/v1/sessions", headers=HEADERS)
        assert r.status_code == 200
        assert r.json() == {"sessions": [], "total": 0}

    async def test_list_returns_submitted(
        self, client: AsyncClient, tmp_path: Path
    ):
        r = await client.post(
            "/api/v1/sessions", json=_spec_body(tmp_path), headers=HEADERS
        )
        sid = r.json()["id"]
        # Give the bg task a moment to complete
        await asyncio.sleep(0.3)
        r2 = await client.get("/api/v1/sessions", headers=HEADERS)
        assert r2.status_code == 200
        ids = [s["id"] for s in r2.json()["sessions"]]
        assert sid in ids

    async def test_get_returns_detail_and_frames(
        self, client: AsyncClient, tmp_path: Path
    ):
        r = await client.post(
            "/api/v1/sessions", json=_spec_body(tmp_path), headers=HEADERS
        )
        sid = r.json()["id"]
        # Poll until completed
        for _ in range(50):
            r = await client.get(f"/api/v1/sessions/{sid}", headers=HEADERS)
            if r.json()["status"] == "completed":
                break
            await asyncio.sleep(0.05)
        body = r.json()
        assert body["status"] == "completed"
        assert body["runtime_id"]
        assert len(body["frames"]) >= 2

    async def test_get_404_for_unknown(self, client: AsyncClient):
        r = await client.get("/api/v1/sessions/does-not-exist", headers=HEADERS)
        assert r.status_code == 404

    async def test_list_filter_by_status(
        self, client: AsyncClient, tmp_path: Path
    ):
        r = await client.post(
            "/api/v1/sessions", json=_spec_body(tmp_path), headers=HEADERS
        )
        sid = r.json()["id"]
        # wait for completion
        for _ in range(50):
            r = await client.get(f"/api/v1/sessions/{sid}", headers=HEADERS)
            if r.json()["status"] == "completed":
                break
            await asyncio.sleep(0.05)
        r2 = await client.get(
            "/api/v1/sessions?status=completed", headers=HEADERS
        )
        assert r2.status_code == 200
        assert any(s["id"] == sid for s in r2.json()["sessions"])
        r3 = await client.get(
            "/api/v1/sessions?status=queued", headers=HEADERS
        )
        assert not any(s["id"] == sid for s in r3.json()["sessions"])


# ── cancel ─────────────────────────────────────────────────────────────


class TestCancel:
    async def test_cancel_completed_is_idempotent(
        self, client: AsyncClient, tmp_path: Path
    ):
        r = await client.post(
            "/api/v1/sessions", json=_spec_body(tmp_path), headers=HEADERS
        )
        sid = r.json()["id"]
        await asyncio.sleep(0.3)
        r2 = await client.post(
            f"/api/v1/sessions/{sid}/cancel",
            json={"reason": "test"},
            headers=HEADERS,
        )
        assert r2.status_code == 202
        assert r2.json()["status"] == "cancelled"

    async def test_cancel_404(self, client: AsyncClient):
        r = await client.post(
            "/api/v1/sessions/nope/cancel", json={"reason": "x"}, headers=HEADERS
        )
        assert r.status_code == 404


# ── HITL endpoints ─────────────────────────────────────────────────────


class TestHITL:
    async def test_list_pending_empty(
        self, client: AsyncClient, tmp_path: Path
    ):
        r = await client.post(
            "/api/v1/sessions", json=_spec_body(tmp_path), headers=HEADERS
        )
        sid = r.json()["id"]
        r2 = await client.get(
            f"/api/v1/sessions/{sid}/hitl/pending", headers=HEADERS
        )
        assert r2.status_code == 200
        assert r2.json()["pending"] == []

    async def test_resolve_unknown_409(
        self, client: AsyncClient
    ):
        r = await client.post(
            "/api/v1/sessions/sxxx/hitl/r0",
            json={"decision": "accept"},
            headers=HEADERS,
        )
        assert r.status_code == 409
