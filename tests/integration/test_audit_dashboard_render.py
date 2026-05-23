"""Plan 8 Task 6 / D8.4 — dashboard audit timeline render tests.

Drives ``GET /dashboard/sessions/{sid}/audit`` through the live
FastAPI app with a dashboard cookie session attached. Two tests:

* ``test_dashboard_audit_timeline_renders`` — alice logs in via the
  bcrypt path, owns ``sess-alice``, and the HTMX endpoint returns
  200 + the rendered ``_session_audit_timeline.html`` fragment
  containing the seeded audit actions.
* ``test_dashboard_audit_timeline_forbidden_non_owner`` — alice
  (cookie session) is NOT the owner of ``sess-bob`` and her label
  ``dashboard-alice`` is not in the admin role; the endpoint
  returns 403 with the ``Forbidden.`` HTMX fragment.

Audit rows are seeded directly through the store — the test
intent is the render + RBAC contract, not the upstream
audit-service plumbing (which Task 5's tests already cover).
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
    """Build a Config with ``alice`` in dashboard_users + no
    ``role_mapping`` for her — so her label resolves to ``viewer``
    and the own-session ownership branch is the only thing that
    can grant access.

    Picking ``viewer`` (the default) for alice forces the negative-
    path test to fall through to the ownership check. If we made
    her ``admin`` she would always be allowed regardless of owner.
    """
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/audit-dash.db"
    cfg.api_keys_raw = ""
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.dashboard_session_secret = SecretStr(
        "test-secret-32-bytes-or-longer-xxxxxxxx"
    )
    cfg.dashboard_users_raw = f"alice={_bcrypt_hash('alice-pw')}"
    # Empty role_mapping means alice resolves to "viewer" (NOT
    # admin via the autouse conftest patch — that patch only fires
    # for paths that set ``request.state.api_key_label``, which
    # /dashboard/* does NOT). This is exactly what we want: the
    # dashboard endpoint's own-session check becomes the sole
    # gate.
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
        # Login alice — the dashboard router sets the cookie
        # session key the cookie middleware later resolves to
        # the ``dashboard-alice`` label.
        r = await ac.post(
            "/dashboard/login",
            data={"username": "alice", "password": "alice-pw"},
        )
        assert r.status_code == 303, r.text
        yield ac, app.state.store


async def _seed_session(
    store: SqlAlchemyStore, *, sid: str, owner: str
) -> None:
    await store.create_session(
        id=sid,
        spec_json={"prompt": "seed"},
        trace_id=None,
        backend="inprocess",
        tags=(),
        owner=owner,
    )


async def test_dashboard_audit_timeline_renders(
    client_and_store,
) -> None:
    """Alice's own session → 200 + the seeded ``submit`` / ``pause``
    audit actions are rendered into the timeline fragment with
    their ``actor`` annotated."""
    client, store = client_and_store
    await _seed_session(store, sid="sess-alice", owner="dashboard-alice")
    for action in ("submit", "pause"):
        await store.record_audit(
            actor="dashboard-alice",
            action=action,
            target_type="session",
            target_id="sess-alice",
            metadata={"reason": "test"},
        )

    r = await client.get("/dashboard/sessions/sess-alice/audit")
    assert r.status_code == 200, r.text
    body = r.text
    # Container + per-item structure (driven by the fragment template).
    assert "audit-timeline" in body
    assert "audit-list" in body
    assert "submit" in body
    assert "pause" in body
    assert "by dashboard-alice" in body
    # Metadata is rendered inside a <details> when non-empty.
    assert "<details" in body
    assert "metadata" in body


async def test_dashboard_audit_timeline_forbidden_non_owner(
    client_and_store,
) -> None:
    """Alice asking for bob's session → 403 + the dashboard's
    inline ``Forbidden.`` HTMX fragment (no JSON, no redirect — the
    fragment swaps directly into the panel target)."""
    client, store = client_and_store
    await _seed_session(store, sid="sess-bob", owner="dashboard-bob")
    await store.record_audit(
        actor="dashboard-bob",
        action="submit",
        target_type="session",
        target_id="sess-bob",
    )

    r = await client.get("/dashboard/sessions/sess-bob/audit")
    assert r.status_code == 403, r.text
    assert "Forbidden." in r.text
