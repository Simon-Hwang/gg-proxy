"""End-to-end smoke: submit → run (inprocess fake runner) → list → get.

Exercises the full FastAPI app stack (lifespan, middleware, routers,
SessionManager, store, EventBus). The real SDK is replaced by a trivial
runner so the test runs without ``ANTHROPIC_API_KEY``; the slot for the
real-SDK path lives in :func:`test_with_real_sdk` below, marked
``requires_api_key``.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.frames import (
    make_msg_chunk,
    make_session_end,
    make_tool_request,
    make_tool_result,
)
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.spec import SessionSpec
from gg_relay.session.transport.protocol import SessionTransport


async def _scripted_runner(transport: SessionTransport, spec: SessionSpec) -> None:
    """A scripted runner that emits a small but realistic frame sequence."""
    del spec
    await transport.send(make_msg_chunk(1, {"text": "starting"}))
    await transport.send(make_tool_request(2, "s:r1", "Echo", {"text": "hi"}))
    await transport.send(make_tool_result(3, "s:r1", "ok", {"text": "hi"}))
    await transport.send(make_session_end(4, "completed", tokens={}, cost_usd=0.0))


def _cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/e2e.db"
    cfg.api_keys_raw = "test-key"
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.dashboard_admin_password = SecretStr("admin")
    cfg.dashboard_session_secret = SecretStr("x" * 32)
    cfg.public_base_url = "http://localhost:8000"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def client(tmp_path: Path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg)

    def _factory(
        kind: str,
        policy: ToolPolicy,
        coordinator: HITLCoordinator,
        session_id: str,
        **kwargs: object,
    ):
        del kind, policy, coordinator, session_id, kwargs
        return InProcessExecutor(runner=_scripted_runner)

    app.state.executor_factory_override = _factory

    from gg_relay.store import create_all_tables, make_async_engine

    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as ac, app.router.lifespan_context(app):
        yield ac


HEADERS = {"X-API-Key": "test-key"}


async def test_submit_run_list_get_full_cycle(
    client: AsyncClient, tmp_path: Path
) -> None:
    """The canonical end-to-end happy path."""
    body = {
        "spec": {
            "prompt": "say OK",
            "cwd": str(tmp_path),
            "plugins": {"profile": "minimal"},
            "executor": "inprocess",
            "timeout_s": 5,
            "tags": ["e2e"],
        },
        "credentials": {"ANTHROPIC_API_KEY": "sk-ant-leakable"},
    }
    # 1. submit
    r = await client.post("/api/v1/sessions", json=body, headers=HEADERS)
    assert r.status_code == 202, r.text
    sid = r.json()["id"]
    # Credentials must not leak in the submit response.
    assert "sk-ant-leakable" not in r.text

    # 2. poll until completed (or fail after generous budget)
    for _ in range(80):
        r = await client.get(f"/api/v1/sessions/{sid}", headers=HEADERS)
        if r.json()["status"] == "completed":
            break
        await asyncio.sleep(0.05)
    detail = r.json()
    assert detail["status"] == "completed"
    assert detail["runtime_id"]
    # Frames persisted: msg.chunk + tool.request + tool.result + session.end
    types = [f["type"] for f in detail["frames"]]
    assert "msg.chunk" in types
    assert "tool.request" in types
    assert "tool.result" in types
    assert "session.end" in types
    # No credential leak in any persisted frame.
    assert "sk-ant-leakable" not in r.text

    # 3. list includes the session
    r = await client.get("/api/v1/sessions", headers=HEADERS)
    ids = [s["id"] for s in r.json()["sessions"]]
    assert sid in ids


@pytest.mark.requires_api_key
async def test_with_real_sdk_when_available(tmp_path: Path) -> None:
    """Hook for running the same flow against the actual Anthropic SDK.

    Cleanly skips unless ``ANTHROPIC_API_KEY`` is exported. Intentionally
    left as a documentation slot — the wiring would mirror the trivial
    runner but use :func:`make_sdk_runner` and a real prompt.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    pytest.skip("real-SDK path requires Plan 3 docker image; see examples/end_to_end_demo.py")
