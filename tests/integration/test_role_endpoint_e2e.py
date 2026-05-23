"""End-to-end role enforcement tests (Plan 8 D8.22 / Task 4).

Three black-box tests that drive a live FastAPI app through
``httpx.AsyncClient`` (matching ``test_api_sessions.py``'s style)
to verify the ``require_role`` dependency wiring on the
session-mutation endpoints.

Each test builds its own ``Config`` with an *explicit*
``role_mapping_raw`` so the root conftest's autouse "empty
role_mapping → admin" patch does NOT activate. This is the same
mechanism real operators use: pass a non-empty
``RELAY_ROLE_MAPPING_RAW`` and the strict production logic kicks
in across the board.

The api-key labels in the role map use the human-readable shape
``label=key`` (``alice-key:alice``) so the auto-derived
``key-<sha256>`` form is bypassed and the role_mapping reads
naturally.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
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
    """Drain immediately — we only need the session row to land in
    the DB so DELETE / cancel paths can do their ownership lookups."""
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


def _make_cfg(
    tmp_path: Path,
    *,
    api_keys_raw: str,
    role_mapping_raw: str,
) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/role.db"
    cfg.api_keys_raw = api_keys_raw
    cfg.role_mapping_raw = role_mapping_raw  # explicit so conftest patch sleeps
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://localhost:8000"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def app_factory(
    tmp_path: Path,
) -> AsyncIterator[Callable[[str, str], Any]]:
    """Factory yielding ``async with`` clients pinned to a fresh app
    per (api_keys_raw, role_mapping_raw) tuple."""
    clients: list[Any] = []

    async def _make(
        api_keys_raw: str, role_mapping_raw: str
    ) -> AsyncClient:
        cfg = _make_cfg(
            tmp_path,
            api_keys_raw=api_keys_raw,
            role_mapping_raw=role_mapping_raw,
        )
        app = create_app(cfg)
        app.state.executor_factory_override = _factory_override()
        from gg_relay.store import create_all_tables, make_async_engine

        eng = make_async_engine(cfg.database_url)
        await create_all_tables(eng)
        await eng.dispose()
        transport = ASGITransport(app=app)
        client_ctx = AsyncClient(transport=transport, base_url="http://test")
        lifespan_ctx = app.router.lifespan_context(app)
        await lifespan_ctx.__aenter__()
        client = await client_ctx.__aenter__()
        clients.append((client_ctx, lifespan_ctx, app))
        return client

    yield _make

    for client_ctx, lifespan_ctx, _app in clients:
        await client_ctx.__aexit__(None, None, None)
        await lifespan_ctx.__aexit__(None, None, None)


def _spec_body(tmp_path: Path) -> dict[str, Any]:
    return {
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


async def test_viewer_post_session_returns_403(
    app_factory: Callable[[str, str], Any],
    tmp_path: Path,
) -> None:
    """A viewer hitting ``POST /sessions`` must 403 with the
    ``insufficient_role`` body shape so a dashboard can render an
    actionable hint."""
    # Use the human-readable ``label=key`` form so the role map
    # reads cleanly.
    client = await app_factory(
        "viewer-user=viewer-key",
        "viewer-user=viewer",
    )
    r = await client.post(
        "/api/v1/sessions",
        json=_spec_body(tmp_path),
        headers={"X-API-Key": "viewer-key"},
    )
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "insufficient_role"
    assert detail["required_role"] == "submitter"
    assert detail["current_role"] == "viewer"


async def test_submitter_post_session_succeeds(
    app_factory: Callable[[str, str], Any],
    tmp_path: Path,
) -> None:
    """A submitter hitting ``POST /sessions`` must succeed (202)."""
    client = await app_factory(
        "submitter-user=submitter-key",
        "submitter-user=submitter",
    )
    r = await client.post(
        "/api/v1/sessions",
        json=_spec_body(tmp_path),
        headers={"X-API-Key": "submitter-key"},
    )
    assert r.status_code == 202, r.text
    assert "id" in r.json()


async def test_admin_can_cancel_others_session(
    app_factory: Callable[[str, str], Any],
    tmp_path: Path,
) -> None:
    """An admin must be able to cancel a session that was
    submitted by a different (submitter) user. This is the
    inverse of the unit test ``test_submitter_cannot_cancel_others_session``
    — together they pin both halves of the own-session policy."""
    client = await app_factory(
        "alice=alice-key,bob=bob-key",
        "alice=admin,bob=submitter",
    )
    # bob submits → row.owner == "bob"
    r = await client.post(
        "/api/v1/sessions",
        json=_spec_body(tmp_path),
        headers={"X-API-Key": "bob-key"},
    )
    assert r.status_code == 202, r.text
    sid = r.json()["id"]

    # alice (admin) cancels bob's session — own-session check
    # is bypassed by the admin role check.
    r2 = await client.post(
        f"/api/v1/sessions/{sid}/cancel",
        json={"reason": "admin-cancel"},
        headers={"X-API-Key": "alice-key"},
    )
    assert r2.status_code == 202, r2.text
    assert r2.json()["status"] == "cancelled"
