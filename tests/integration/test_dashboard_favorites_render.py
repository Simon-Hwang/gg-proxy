"""Plan 8 Task 13 / D8.21 — dashboard favorites page render test."""
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
from gg_relay.store import SqlAlchemyStore, create_all_tables, make_async_engine


async def _trivial_runner(transport: SessionTransport, spec: SessionSpec) -> None:
    del spec
    await transport.send(make_msg_chunk(1, {"x": 1}))
    await transport.send(
        make_session_end(2, "completed", tokens={}, cost_usd=0.0)
    )


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
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")


def _make_cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/favorites-dash.db"
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
async def client_and_store(tmp_path: Path):
    cfg = _make_cfg(tmp_path)
    app = create_app(cfg)
    app.state.executor_factory_override = _factory()

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


async def _seed_session(
    store: SqlAlchemyStore,
    *,
    sid: str,
    owner: str,
    prompt: str = "favored prompt",
) -> None:
    await store.create_session(
        id=sid,
        spec_json={"prompt": prompt},
        trace_id=None,
        backend="inprocess",
        tags=(),
        owner=owner,
    )


async def test_favorites_page_renders_starred_sessions(
    client_and_store,
) -> None:
    """Two seeded favorites → 200 + both rows in the table + the
    unstar button wired to the API endpoint."""
    client, store = client_and_store
    await _seed_session(
        store, sid="sess-fav-1", owner="dashboard-alice", prompt="alice prompt 1"
    )
    await _seed_session(
        store, sid="sess-fav-2", owner="dashboard-alice", prompt="alice prompt 2"
    )
    await store.add_favorite(
        session_id="sess-fav-1", user_label="dashboard-alice"
    )
    await store.add_favorite(
        session_id="sess-fav-2", user_label="dashboard-alice"
    )

    r = await client.get("/dashboard/favorites")
    assert r.status_code == 200, r.text
    body = r.text
    assert "My Favorites" in body
    assert 'id="favorites-table"' in body
    assert "sess-fav-1" in body
    assert "sess-fav-2" in body
    assert "alice prompt 1" in body
    assert "alice prompt 2" in body
    assert (
        'hx-delete="/api/v1/sessions/sess-fav-1/favorite"' in body
    )
    assert (
        'hx-delete="/api/v1/sessions/sess-fav-2/favorite"' in body
    )


async def test_favorites_page_empty_state(client_and_store) -> None:
    """No favorites → 200 + the ``No favorites yet.`` empty state."""
    client, _store = client_and_store
    r = await client.get("/dashboard/favorites")
    assert r.status_code == 200, r.text
    assert "No favorites yet." in r.text
