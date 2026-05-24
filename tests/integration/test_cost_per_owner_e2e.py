"""End-to-end tests for ``/api/v1/cost/*`` (Plan 8 Task 23 / D8.30).

Black-box tests driving a live FastAPI app via ``httpx.AsyncClient``,
following the ``test_audit_endpoint_e2e.py`` style. The app boots
with an explicit ``role_mapping_raw`` so the autouse conftest patch
(which grants ``admin`` to any authenticated request when the
mapping is empty) is bypassed and production RBAC runs.

Tests:

  * ``test_alice_3_sessions_bob_2_sessions_grouped_correctly`` —
    seed five sessions, then verify
    ``GET /api/v1/cost/per-owner`` returns the right per-owner
    counts and sums (and respects ``order_by=cost`` default).
  * ``test_csv_export_writes_audit`` — admin downloads the CSV;
    the response carries the expected MIME + header AND the
    audit log records a ``cost_export`` action attributed to the
    admin.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from gg_relay.api.main import create_app
from gg_relay.api.routers.cost import _clear_summary_cache
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
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/cost-e2e.db"
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
    """Yield a factory that returns ``(client, store)`` pairs bound
    to a fresh app + DB so each test seeds rows in isolation.

    Lifespan attaches ``app.state.store`` already so the factory
    reuses it — seeds and reads land in the same engine the router
    queries against.
    """
    clients: list[Any] = []
    _clear_summary_cache()

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
        store: SqlAlchemyStore = app.state.store
        clients.append((client_ctx, lifespan_ctx, app))
        return client, store

    yield _make

    for client_ctx, lifespan_ctx, _app in clients:
        await client_ctx.__aexit__(None, None, None)
        await lifespan_ctx.__aexit__(None, None, None)
    _clear_summary_cache()


async def _seed_session(
    store: SqlAlchemyStore,
    *,
    sid: str,
    owner: str,
    cost: float,
) -> None:
    """Insert one queued session + write the aggregate cost.

    Two steps mirror production: SessionManager creates the row in
    ``queued`` state, then writes the aggregates at terminal
    transition. We do both inline so the cost column carries the
    expected value.
    """
    await store.create_session(
        id=sid,
        spec_json={"prompt": "seed"},
        trace_id=None,
        backend="inprocess",
        tags=(),
        owner=owner,
    )
    await store.update_session_aggregates(sid, cost_usd=cost)


async def test_alice_3_sessions_bob_2_sessions_grouped_correctly(
    app_factory: Callable[[str, str], Any],
) -> None:
    """Five sessions (3 alice + 2 bob) → per-owner GROUP BY returns
    the right counts and sums.

    Admin role is required for the unrestricted view; the same
    request as a submitter would force-filter to its own owner (the
    unit test pins that path — here we just want the SQL GROUP BY
    + ordering contract end-to-end).
    """
    client, store = await app_factory(
        "alice-key:alice", "alice=admin"
    )
    await _seed_session(store, sid="s-a1", owner="alice", cost=0.10)
    await _seed_session(store, sid="s-a2", owner="alice", cost=0.20)
    await _seed_session(store, sid="s-a3", owner="alice", cost=0.30)
    await _seed_session(store, sid="s-b1", owner="bob", cost=2.50)
    await _seed_session(store, sid="s-b2", owner="bob", cost=2.50)

    r = await client.get(
        "/api/v1/cost/per-owner",
        headers={"X-API-Key": "alice-key"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    items = body["items"]
    by_owner = {it["owner"]: it for it in items}
    assert by_owner["alice"]["session_count"] == 3
    assert by_owner["alice"]["total_cost_usd"] == pytest.approx(0.60)
    assert by_owner["bob"]["session_count"] == 2
    assert by_owner["bob"]["total_cost_usd"] == pytest.approx(5.0)
    # Default ordering is cost DESC: bob (5.0) precedes alice (0.6).
    assert items[0]["owner"] == "bob"
    assert items[1]["owner"] == "alice"


async def test_csv_export_writes_audit(
    app_factory: Callable[[str, str], Any],
) -> None:
    """Admin downloads the CSV; ``audit_log`` records the action.

    The admin role is required (submitter would 403). We assert:

      1. Response is ``text/csv`` with a ``Content-Disposition``
         header naming the export.
      2. The CSV body begins with the expected header row +
         contains the seeded owners.
      3. ``GET /api/v1/audit`` (also admin) returns a row with
         ``action='cost_export'`` attributed to alice. Mirrors
         the audit-on-mutation contract Plan 8 D8.4 / Task 5
         pinned for the other mutation endpoints.
    """
    client, store = await app_factory(
        "alice-key:alice", "alice=admin"
    )
    await _seed_session(store, sid="exp-1", owner="alice", cost=1.0)
    await _seed_session(store, sid="exp-2", owner="bob", cost=2.0)

    r = await client.get(
        "/api/v1/cost/export.csv",
        headers={"X-API-Key": "alice-key"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0] == "owner,session_count,total_cost_usd"
    # Order is cost DESC by default — bob (2.0) before alice (1.0).
    csv_body = "\n".join(lines[1:])
    assert "bob" in csv_body
    assert "alice" in csv_body

    audit_r = await client.get(
        "/api/v1/audit?action=cost_export",
        headers={"X-API-Key": "alice-key"},
    )
    assert audit_r.status_code == 200, audit_r.text
    audit_items = audit_r.json()["items"]
    assert len(audit_items) == 1
    rec = audit_items[0]
    assert rec["actor"] == "alice"
    assert rec["action"] == "cost_export"
    assert rec["target_type"] == "date_range"


# ``pytest.approx`` import sits at the bottom because the file
# is otherwise free of pytest imports beyond the asyncio
# fixture — keeping it module-local avoids polluting the
# integration-test top-level imports.
import pytest  # noqa: E402  isort:skip
