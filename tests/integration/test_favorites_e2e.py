"""Session favorites end-to-end tests — Plan 8 D8.21 / Task 13.

Black-box tests through ``httpx.AsyncClient`` against a live
FastAPI app (mirrors :mod:`tests.integration.test_comments_e2e`).
Each test seeds its own role mapping so the root conftest's
"empty role_mapping → admin" autouse patch sleeps and the
production role enforcement kicks in.
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
    """Drain immediately so the session row materialises and the
    favorite endpoints can find their parent."""
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
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/favorites.db"
    cfg.api_keys_raw = api_keys_raw
    cfg.role_mapping_raw = role_mapping_raw
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
    """Factory yielding ``AsyncClient`` instances pinned to a fresh
    app per (api_keys_raw, role_mapping_raw) tuple."""
    clients: list[Any] = []

    async def _make(
        api_keys_raw: str,
        role_mapping_raw: str,
    ) -> tuple[AsyncClient, Any]:
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
        clients.append((client_ctx, lifespan_ctx))
        return client, app

    yield _make

    for client_ctx, lifespan_ctx in clients:
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


async def _submit_session(
    client: AsyncClient, tmp_path: Path, key: str
) -> str:
    r = await client.post(
        "/api/v1/sessions",
        json=_spec_body(tmp_path),
        headers={"X-API-Key": key},
    )
    assert r.status_code == 202, r.text
    return r.json()["id"]


async def test_star_creates_audit_session_star(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """POST → 204 + a ``session_star`` audit row attributed to alice."""
    client, app = await app_factory(
        "alice=alice-key",
        "alice=submitter",
    )
    sid = await _submit_session(client, tmp_path, "alice-key")

    r = await client.post(
        f"/api/v1/sessions/{sid}/favorite",
        headers={"X-API-Key": "alice-key"},
    )
    assert r.status_code == 204, r.text

    store = app.state.store
    rows, _ = await store.list_audit(
        session_id=sid, action="session_star", limit=10
    )
    assert len(rows) == 1, f"expected 1 session_star row, got {rows!r}"
    assert rows[0]["actor"] == "alice"
    assert rows[0]["target_type"] == "session"
    assert rows[0]["target_id"] == sid


async def test_star_idempotent_no_double_audit(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """Second star is a 204 no-op; the audit log still has exactly
    one ``session_star`` row (idempotency contract — only actual
    state changes write audit rows)."""
    client, app = await app_factory(
        "alice=alice-key",
        "alice=submitter",
    )
    sid = await _submit_session(client, tmp_path, "alice-key")

    r1 = await client.post(
        f"/api/v1/sessions/{sid}/favorite",
        headers={"X-API-Key": "alice-key"},
    )
    r2 = await client.post(
        f"/api/v1/sessions/{sid}/favorite",
        headers={"X-API-Key": "alice-key"},
    )
    assert r1.status_code == 204
    assert r2.status_code == 204, "second star must collapse to 204"

    store = app.state.store
    rows, _ = await store.list_audit(
        session_id=sid, action="session_star", limit=10
    )
    assert len(rows) == 1, (
        f"second star must NOT write a second audit row: {rows!r}"
    )


async def test_unstar_removes_record_and_audits(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """Star → un-star round-trip writes both audit rows; the
    favorite is gone from the list endpoint after un-star."""
    client, app = await app_factory(
        "alice=alice-key",
        "alice=submitter",
    )
    sid = await _submit_session(client, tmp_path, "alice-key")

    await client.post(
        f"/api/v1/sessions/{sid}/favorite",
        headers={"X-API-Key": "alice-key"},
    )

    g1 = await client.get(
        "/api/v1/sessions/favorites",
        headers={"X-API-Key": "alice-key"},
    )
    assert g1.status_code == 200, g1.text
    body1 = g1.json()
    assert body1["user"] == "alice"
    assert [it["session_id"] for it in body1["items"]] == [sid]

    d = await client.delete(
        f"/api/v1/sessions/{sid}/favorite",
        headers={"X-API-Key": "alice-key"},
    )
    assert d.status_code == 204, d.text

    g2 = await client.get(
        "/api/v1/sessions/favorites",
        headers={"X-API-Key": "alice-key"},
    )
    assert g2.json()["items"] == []

    store = app.state.store
    rows, _ = await store.list_audit(session_id=sid, limit=20)
    actions = [r["action"] for r in rows]
    assert "session_star" in actions
    assert "session_unstar" in actions


async def test_unstar_idempotent_no_audit_on_noop(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """DELETE on a never-starred session is a 204 no-op WITHOUT
    writing a spurious ``session_unstar`` audit row."""
    client, app = await app_factory(
        "alice=alice-key",
        "alice=submitter",
    )
    sid = await _submit_session(client, tmp_path, "alice-key")

    r = await client.delete(
        f"/api/v1/sessions/{sid}/favorite",
        headers={"X-API-Key": "alice-key"},
    )
    assert r.status_code == 204, r.text

    store = app.state.store
    rows, _ = await store.list_audit(
        session_id=sid, action="session_unstar", limit=10
    )
    assert rows == [], (
        f"un-star no-op must NOT write an audit row: {rows!r}"
    )


async def test_list_my_favorites_excludes_others(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """Alice stars sid-A; bob's GET /favorites is empty (per-user
    scoping). The admin path (?user=) is exercised separately."""
    client, _app = await app_factory(
        "alice=alice-key,bob=bob-key",
        "alice=submitter,bob=submitter",
    )
    sid_a = await _submit_session(client, tmp_path, "alice-key")
    r = await client.post(
        f"/api/v1/sessions/{sid_a}/favorite",
        headers={"X-API-Key": "alice-key"},
    )
    assert r.status_code == 204

    g_alice = await client.get(
        "/api/v1/sessions/favorites",
        headers={"X-API-Key": "alice-key"},
    )
    assert {it["session_id"] for it in g_alice.json()["items"]} == {sid_a}

    g_bob = await client.get(
        "/api/v1/sessions/favorites",
        headers={"X-API-Key": "bob-key"},
    )
    body_bob = g_bob.json()
    assert body_bob["user"] == "bob"
    assert body_bob["items"] == []

    g_bob_probe = await client.get(
        "/api/v1/sessions/favorites?user=alice",
        headers={"X-API-Key": "bob-key"},
    )
    assert g_bob_probe.json()["user"] == "bob"
    assert g_bob_probe.json()["items"] == []


async def test_admin_can_inspect_other_user_favorites(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """Admin may pass ``?user=<label>`` to inspect another user's
    favorites (moderation / debugging)."""
    client, _app = await app_factory(
        "alice=alice-key,bob=bob-key",
        "alice=submitter,bob=admin",
    )
    sid = await _submit_session(client, tmp_path, "alice-key")
    await client.post(
        f"/api/v1/sessions/{sid}/favorite",
        headers={"X-API-Key": "alice-key"},
    )

    g = await client.get(
        "/api/v1/sessions/favorites?user=alice",
        headers={"X-API-Key": "bob-key"},
    )
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["user"] == "alice"
    assert {it["session_id"] for it in body["items"]} == {sid}


async def test_star_unknown_session_returns_404(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """POST against a non-existent sid surfaces the structured
    ``session_not_found`` detail."""
    client, _app = await app_factory(
        "alice=alice-key",
        "alice=submitter",
    )
    r = await client.post(
        "/api/v1/sessions/sid-does-not-exist/favorite",
        headers={"X-API-Key": "alice-key"},
    )
    assert r.status_code == 404, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "session_not_found"
