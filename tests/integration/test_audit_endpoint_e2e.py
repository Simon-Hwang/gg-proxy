"""End-to-end tests for ``GET /api/v1/audit`` (Plan 8 Task 6 / D8.4).

Four black-box tests driving a live FastAPI app via
``httpx.AsyncClient`` (matching the ``test_role_endpoint_e2e.py``
style). Each test seeds audit rows directly through the store
(no need to drive a real session lifecycle — the audit row shape
is fully owned by :meth:`SqlAlchemyStore.record_audit`):

* ``test_audit_endpoint_filters_by_session_id`` — admin lists audit
  for one of two sessions and sees only the matching rows.
* ``test_audit_endpoint_cursor_pagination`` — admin pages through
  60 rows with ``limit=50`` and the cursor delivers the tail.
* ``test_audit_endpoint_non_admin_sees_only_own_actor`` — submitter
  without ``session_id`` is force-filtered to their own actor.
* ``test_audit_endpoint_non_admin_session_id_must_own`` — submitter
  asking for an admin's session is 403'd.

Every test seeds an explicit ``role_mapping_raw`` so the autouse
conftest patch (which grants ``admin`` to authenticated requests
when ``role_mapping`` is empty) is bypassed and the production
RBAC logic actually runs.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
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


def _make_cfg(
    tmp_path: Path,
    *,
    api_keys_raw: str,
    role_mapping_raw: str,
) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/audit-e2e.db"
    cfg.api_keys_raw = api_keys_raw
    # Explicit non-empty role_mapping bypasses the conftest autouse
    # safety-net patch and forces production RBAC.
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
    """Factory yielding (client, store) tuples bound to a fresh app
    + fresh sqlite DB per (api_keys_raw, role_mapping_raw) pair so
    every test can seed its own audit rows in isolation.

    Returns ``(client, store)`` so the test can both drive HTTP
    *and* seed rows through the store without going through the
    full audit-service stack (which would couple the test to the
    middleware ordering).
    """
    clients: list[Any] = []

    async def _make(
        api_keys_raw: str, role_mapping_raw: str
    ) -> tuple[AsyncClient, SqlAlchemyStore]:
        cfg = _make_cfg(
            tmp_path,
            api_keys_raw=api_keys_raw,
            role_mapping_raw=role_mapping_raw,
        )
        app = create_app(cfg)
        app.state.executor_factory_override = _factory_override()

        eng = make_async_engine(cfg.database_url)
        await create_all_tables(eng)
        await eng.dispose()
        transport = ASGITransport(app=app)
        client_ctx = AsyncClient(transport=transport, base_url="http://test")
        lifespan_ctx = app.router.lifespan_context(app)
        await lifespan_ctx.__aenter__()
        client = await client_ctx.__aenter__()
        # The lifespan attached ``app.state.store`` already; reuse it
        # so seeds and reads land in the SAME engine instance the
        # router queries.
        store: SqlAlchemyStore = app.state.store
        clients.append((client_ctx, lifespan_ctx, app))
        return client, store

    yield _make

    for client_ctx, lifespan_ctx, _app in clients:
        await client_ctx.__aexit__(None, None, None)
        await lifespan_ctx.__aexit__(None, None, None)


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


async def test_audit_endpoint_filters_by_session_id(
    app_factory: Callable[[str, str], Any],
) -> None:
    """Admin lists audit for session A; rows for session B do not
    leak through. The ``session_id`` filter is an alias for
    ``target_type='session' + target_id=<sid>``, so this also pins
    the alias contract end-to-end."""
    client, store = await app_factory(
        "alice-key:alice",
        "alice=admin",
    )
    await _seed_session(store, sid="sess-A", owner="alice")
    await _seed_session(store, sid="sess-B", owner="alice")
    for action in ("submit", "pause", "resume"):
        await store.record_audit(
            actor="alice",
            action=action,
            target_type="session",
            target_id="sess-A",
        )
    for action in ("submit", "cancel"):
        await store.record_audit(
            actor="alice",
            action=action,
            target_type="session",
            target_id="sess-B",
        )

    r = await client.get(
        "/api/v1/audit?session_id=sess-A",
        headers={"X-API-Key": "alice-key"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    items = body["items"]
    assert len(items) == 3, items
    assert {it["action"] for it in items} == {"submit", "pause", "resume"}
    assert all(it["target_id"] == "sess-A" for it in items)


async def test_audit_endpoint_cursor_pagination(
    app_factory: Callable[[str, str], Any],
) -> None:
    """Seed 60 rows; first page with ``limit=50`` returns 50 +
    cursor; second page returns the remaining 10. Cursor opacity is
    enforced upstream — we just verify the wire contract."""
    client, store = await app_factory(
        "alice-key:alice",
        "alice=admin",
    )
    await _seed_session(store, sid="sess-many", owner="alice")
    # Use a monotonically increasing ts so list_audit's `ts DESC, id
    # DESC` ordering is deterministic across the two pages. Without
    # this, multiple inserts inside the same microsecond would tie
    # on ts and the id-tiebreaker would still be deterministic but
    # less obviously testable.
    base = datetime.now(UTC) - timedelta(seconds=120)
    for i in range(60):
        await store.record_audit(
            actor="alice",
            action="touch",
            target_type="session",
            target_id="sess-many",
            ts=base + timedelta(seconds=i),
        )

    r1 = await client.get(
        "/api/v1/audit?session_id=sess-many&limit=50",
        headers={"X-API-Key": "alice-key"},
    )
    assert r1.status_code == 200, r1.text
    page1 = r1.json()
    assert len(page1["items"]) == 50
    assert page1["has_more"] is True
    assert page1["next_cursor"] is not None

    r2 = await client.get(
        f"/api/v1/audit?session_id=sess-many&limit=50&after={page1['next_cursor']}",
        headers={"X-API-Key": "alice-key"},
    )
    assert r2.status_code == 200, r2.text
    page2 = r2.json()
    assert len(page2["items"]) == 10
    assert page2["has_more"] is False
    assert page2["next_cursor"] is None
    # No overlap between the two pages — every id is unique.
    ids_p1 = {it["id"] for it in page1["items"]}
    ids_p2 = {it["id"] for it in page2["items"]}
    assert ids_p1.isdisjoint(ids_p2)
    assert len(ids_p1 | ids_p2) == 60


async def test_audit_endpoint_non_admin_sees_only_own_actor(
    app_factory: Callable[[str, str], Any],
) -> None:
    """Submitter ``bob`` calling ``GET /audit`` (no session_id) is
    force-filtered to ``actor='bob'`` — alice's rows are excluded
    even though the audit_log table holds both."""
    client, store = await app_factory(
        "alice-key:alice,bob-key:bob",
        "alice=admin,bob=submitter",
    )
    # Seed rows for both actors; no session_id required for this
    # test — the actor filter is what matters.
    for action in ("submit", "pause"):
        await store.record_audit(actor="alice", action=action)
    for action in ("submit", "cancel", "resume"):
        await store.record_audit(actor="bob", action=action)

    r = await client.get(
        "/api/v1/audit",
        headers={"X-API-Key": "bob-key"},
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 3
    assert all(it["actor"] == "bob" for it in items)

    # And explicitly asking for someone else's actor → 403.
    r403 = await client.get(
        "/api/v1/audit?actor=alice",
        headers={"X-API-Key": "bob-key"},
    )
    assert r403.status_code == 403, r403.text
    detail = r403.json()["detail"]
    assert detail["code"] == "forbidden_audit_filter"
    assert detail["required_role"] == "admin"
    assert detail["current_role"] == "submitter"


async def test_audit_endpoint_non_admin_session_id_must_own(
    app_factory: Callable[[str, str], Any],
) -> None:
    """Submitter ``bob`` listing an admin-owned session is 403'd with
    ``forbidden_audit_view``; a genuinely-missing session id returns
    404 with ``session_not_found`` so the "no such row" and
    "wrong owner" branches stay distinguishable."""
    client, store = await app_factory(
        "alice-key:alice,bob-key:bob",
        "alice=admin,bob=submitter",
    )
    await _seed_session(store, sid="sess-alice", owner="alice")
    await store.record_audit(
        actor="alice",
        action="submit",
        target_type="session",
        target_id="sess-alice",
    )

    r403 = await client.get(
        "/api/v1/audit?session_id=sess-alice",
        headers={"X-API-Key": "bob-key"},
    )
    assert r403.status_code == 403, r403.text
    detail = r403.json()["detail"]
    assert detail["code"] == "forbidden_audit_view"
    assert detail["required_role"] == "admin"
    assert detail["current_role"] == "submitter"
    assert detail["session_owner"] == "alice"

    r404 = await client.get(
        "/api/v1/audit?session_id=ghost",
        headers={"X-API-Key": "bob-key"},
    )
    assert r404.status_code == 404, r404.text
    assert r404.json()["detail"]["code"] == "session_not_found"
