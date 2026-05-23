"""End-to-end tests for ``GET /api/v1/sessions/search`` + dashboard render.

Plan 8 D8.20 / Task 12. Three black-box tests covering the search
contract end-to-end:

* ``test_search_endpoint_basic_filter`` — admin searches by ``q`` and
  only matching prompts surface.
* ``test_search_endpoint_non_admin_forces_own_owner`` — submitter
  without an explicit ``owner`` filter is silently force-filtered to
  their own label; an explicit cross-owner filter returns 403.
* ``test_search_endpoint_dashboard_render`` — the HTMX fragment
  endpoint ``GET /dashboard/search/results`` renders the result table
  for a logged-in user.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
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
from gg_relay.store import SqlAlchemyStore, create_all_tables, make_async_engine


async def _trivial_runner(transport: SessionTransport, spec: SessionSpec) -> None:
    del spec
    await transport.send(make_msg_chunk(1, {"x": 1}))
    await transport.send(
        make_session_end(2, "completed", tokens={}, cost_usd=0.0)
    )


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


def _bcrypt_hash(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")


def _make_cfg(
    tmp_path: Path,
    *,
    api_keys_raw: str = "",
    role_mapping_raw: str = "",
    dashboard_users_raw: str = "",
) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/search-e2e.db"
    cfg.api_keys_raw = api_keys_raw
    cfg.role_mapping_raw = role_mapping_raw
    cfg.dashboard_users_raw = dashboard_users_raw
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.dashboard_session_secret = SecretStr(
        "test-secret-32-bytes-or-longer-xxxxxxxx"
    )
    cfg.public_base_url = "http://localhost:8000"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def app_factory(
    tmp_path: Path,
) -> AsyncIterator[Callable[..., Any]]:
    """Yield a factory that builds a fresh app + sqlite DB per test.

    Returns ``(client, store)`` so the test can seed rows through the
    store *and* drive the HTTP surface — same pattern as
    ``test_audit_endpoint_e2e.py``.
    """
    clients: list[Any] = []

    async def _make(
        *,
        api_keys_raw: str = "",
        role_mapping_raw: str = "",
        dashboard_users_raw: str = "",
    ) -> tuple[AsyncClient, SqlAlchemyStore]:
        cfg = _make_cfg(
            tmp_path,
            api_keys_raw=api_keys_raw,
            role_mapping_raw=role_mapping_raw,
            dashboard_users_raw=dashboard_users_raw,
        )
        app = create_app(cfg)
        app.state.executor_factory_override = _factory_override()

        eng = make_async_engine(cfg.database_url)
        await create_all_tables(eng)
        await eng.dispose()
        transport = ASGITransport(app=app)
        client_ctx = AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        )
        lifespan_ctx = app.router.lifespan_context(app)
        await lifespan_ctx.__aenter__()
        client = await client_ctx.__aenter__()
        store: SqlAlchemyStore = app.state.store
        clients.append((client_ctx, lifespan_ctx, app))
        return client, store

    yield _make

    for client_ctx, lifespan_ctx, _app in clients:
        await client_ctx.__aexit__(None, None, None)
        await lifespan_ctx.__aexit__(None, None, None)


async def _seed_session(
    store: SqlAlchemyStore,
    *,
    sid: str,
    owner: str,
    prompt: str = "seed",
    status: str | None = None,
    tags: tuple[str, ...] = (),
) -> None:
    await store.create_session(
        id=sid,
        spec_json={"prompt": prompt},
        trace_id=None,
        backend="inprocess",
        tags=tags,
        owner=owner,
    )
    if status is not None:
        await store.update_session_status(sid, status=status)


async def test_search_endpoint_basic_filter(
    app_factory: Callable[..., Any],
) -> None:
    """Admin alice searches ``?q=hello``; only rows whose prompt
    contains ``hello`` come back. ``has_more`` mirrors next_cursor."""
    client, store = await app_factory(
        api_keys_raw="alice-key:alice",
        role_mapping_raw="alice=admin",
    )
    await _seed_session(store, sid="sa", owner="alice", prompt="hello world")
    await _seed_session(store, sid="sb", owner="alice", prompt="goodbye")
    await _seed_session(
        store, sid="sc", owner="alice", prompt="HELLO again"
    )

    r = await client.get(
        "/api/v1/sessions/search?q=hello",
        headers={"X-API-Key": "alice-key"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    items = body["items"]
    assert {it["id"] for it in items} == {"sa", "sc"}


async def test_search_endpoint_non_admin_forces_own_owner(
    app_factory: Callable[..., Any],
) -> None:
    """Submitter bob calling ``/search`` without owner is force-filtered
    to ``owner='bob'`` — alice's rows do not leak. Asking for
    ``owner=alice`` explicitly returns 403 ``forbidden_search_owner``."""
    client, store = await app_factory(
        api_keys_raw="alice-key:alice,bob-key:bob",
        role_mapping_raw="alice=admin,bob=submitter",
    )
    await _seed_session(
        store, sid="alice-1", owner="alice", prompt="needle"
    )
    await _seed_session(
        store, sid="bob-1", owner="bob", prompt="needle"
    )
    await _seed_session(
        store, sid="bob-2", owner="bob", prompt="other"
    )

    r_self = await client.get(
        "/api/v1/sessions/search?q=needle",
        headers={"X-API-Key": "bob-key"},
    )
    assert r_self.status_code == 200, r_self.text
    items = r_self.json()["items"]
    assert {it["id"] for it in items} == {"bob-1"}

    r_cross = await client.get(
        "/api/v1/sessions/search?owner=alice",
        headers={"X-API-Key": "bob-key"},
    )
    assert r_cross.status_code == 403, r_cross.text
    detail = r_cross.json()["detail"]
    assert detail["code"] == "forbidden_search_owner"
    assert detail["required_role"] == "admin"
    assert detail["current_role"] == "submitter"


async def test_search_endpoint_dashboard_render(
    tmp_path: Path,
) -> None:
    """``GET /dashboard/search/results?q=...`` renders the results table
    fragment for a logged-in dashboard user — confirms the HTMX
    response-fragment plumbing end-to-end."""
    cfg = _make_cfg(
        tmp_path,
        api_keys_raw="",
        role_mapping_raw="dashboard-alice=admin",
        dashboard_users_raw=f"alice={_bcrypt_hash('alice-pw')}",
    )
    app = create_app(cfg)
    app.state.executor_factory_override = _factory_override()

    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
    ) as ac, app.router.lifespan_context(app):
        login = await ac.post(
            "/dashboard/login",
            data={"username": "alice", "password": "alice-pw"},
        )
        assert login.status_code == 303, login.text

        store: SqlAlchemyStore = app.state.store
        await _seed_session(
            store, sid="d1", owner="dashboard-alice", prompt="hello dash"
        )
        await _seed_session(
            store, sid="d2", owner="dashboard-alice", prompt="off-topic"
        )

        r = await ac.get(
            "/dashboard/search/results?q=hello",
        )
        assert r.status_code == 200, r.text
        body = r.text
        assert "search-table" in body
        assert "hello dash" in body
        assert "/dashboard/sessions/d1" in body
        # Off-topic row does NOT leak into the fragment.
        assert "off-topic" not in body
