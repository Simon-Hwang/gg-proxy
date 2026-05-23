"""POST /api/v1/hitl/batch end-to-end tests — Plan 8 D8.6 / Task 9.

Black-box tests against the live FastAPI app. Pattern + fixtures
mirror :mod:`tests.integration.test_hitl_concurrency`: seed a
``pending`` HITL row + register the coordinator future, then drive
the batch endpoint as the operator would.

Covered:

  * ``test_hitl_batch_approve_partial`` — three hids; one wins via
    the batch, one was already-resolved out-of-band so surfaces as
    ``error_code='hitl_already_resolved'``, one missing → returns
    200 with summary 1 ok + 2 error (the missing id falls into the
    ``hitl_not_pending`` bucket since the coordinator has no entry).
  * ``test_hitl_batch_max_50`` — 51 ids → 422; the boundary 50 is
    accepted (and surfaces 50 ``hitl_not_pending`` errors because
    nothing is registered).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.executor.protocol import ExecutorBackend
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.spec import SessionSpec
from gg_relay.session.transport.protocol import SessionTransport

pytestmark = pytest.mark.asyncio

HEADERS = {"X-API-Key": "k1"}


async def _trivial_runner(transport: SessionTransport, spec: SessionSpec) -> None:
    del transport, spec


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
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/hitlbatch.db"
    cfg.api_keys_raw = "k1"
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://localhost:8000"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def app_bundle(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, HITLCoordinator, Any]]:
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
        coord: HITLCoordinator = app.state.coordinator
        store = app.state.store
        yield ac, coord, store


async def _seed_pending_hitl(
    store: Any,
    coord: HITLCoordinator,
    *,
    sid: str,
    full_req_id: str,
) -> asyncio.Task[Any]:
    """Insert a queued session + a pending HITL row + register the
    coordinator future. Returns the coordinator task so the test can
    await it once the batch resolve drains it.
    """
    await store.create_session(
        id=sid, spec_json={}, trace_id=None, backend="inprocess"
    )
    await store.upsert_hitl(
        id=full_req_id,
        session_id=sid,
        tool="Bash",
        args_json={"cmd": "ls"},
        status="pending",
        created_at=datetime.now(UTC),
    )
    task = asyncio.create_task(
        coord.request(
            full_req_id, tool="Bash", args={"cmd": "ls"}, session_id=sid
        )
    )
    await asyncio.sleep(0)
    return task


async def test_hitl_batch_approve_partial(
    app_bundle: tuple[AsyncClient, HITLCoordinator, Any],
) -> None:
    """Three ids; the well-formed one approves, the resolved-out-of-
    band one and the unknown one both surface as errors so the
    summary lands at ok=1 / error=2.
    """
    ac, coord, store = app_bundle

    # 1) Pending HITL we expect to drain via the batch endpoint.
    sid_ok = "s-ok"
    hid_ok = f"{sid_ok}:r-ok"
    pending_ok = await _seed_pending_hitl(
        store, coord, sid=sid_ok, full_req_id=hid_ok
    )

    # 2) Pending HITL that we resolve out-of-band BEFORE the batch
    #    runs so the coordinator's defence-in-depth raises
    #    HITLAlreadyResolved on the batch path.
    sid_resolved = "s-resolved"
    hid_resolved = f"{sid_resolved}:r-resolved"
    pending_resolved = await _seed_pending_hitl(
        store, coord, sid=sid_resolved, full_req_id=hid_resolved
    )
    # Out-of-band resolve via direct DB UPDATE — flip the row to
    # ``accept`` so the coordinator's pre-flight store check
    # catches it.
    await store.upsert_hitl(
        id=hid_resolved,
        session_id=sid_resolved,
        tool="Bash",
        args_json={"cmd": "ls"},
        status="accept",
        created_at=datetime.now(UTC),
        resolved_at=datetime.now(UTC),
        reason="oob",
        resolver="someone",
    )
    # The pending coordinator future is still alive — drain it
    # explicitly so the test teardown stays clean.
    await coord.cancel_all(reason="test_cleanup", session_id=sid_resolved)

    # 3) Unknown id — never registered with the coordinator.
    hid_missing = "s-missing:r-missing"

    r = await ac.post(
        "/api/v1/hitl/batch",
        json={
            "ids": [hid_ok, hid_resolved, hid_missing],
            "action": "approve",
            "reason": "batch test",
        },
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] == {"ok": 1, "error": 2}
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id[hid_ok]["status"] == "ok"
    # The out-of-band resolved row hits the coordinator's
    # store check first → HITLAlreadyResolved.
    assert by_id[hid_resolved]["status"] == "error"
    assert by_id[hid_resolved]["error_code"] == "hitl_already_resolved"
    # The missing id has no coordinator entry → HITLNotPending.
    assert by_id[hid_missing]["status"] == "error"
    assert by_id[hid_missing]["error_code"] == "hitl_not_pending"

    # Drain the pending coordinator tasks so the test exits cleanly.
    decision_ok = await asyncio.wait_for(pending_ok, timeout=0.5)
    assert decision_ok == "accept"
    decision_resolved = await asyncio.wait_for(pending_resolved, timeout=0.5)
    # cancel_all flipped this future to "deny" with reason
    # ``test_cleanup``.
    assert decision_resolved == "deny"


async def test_hitl_batch_max_50(
    app_bundle: tuple[AsyncClient, HITLCoordinator, Any],
) -> None:
    """51 ids → 422; 50 is accepted (and surfaces 50 errors because
    nothing is registered with the coordinator)."""
    ac, _coord, _store = app_bundle

    too_many = [f"s-x:r-{i}" for i in range(51)]
    r = await ac.post(
        "/api/v1/hitl/batch",
        json={"ids": too_many, "action": "approve"},
        headers=HEADERS,
    )
    assert r.status_code == 422, r.text

    boundary = [f"s-x:r-{i}" for i in range(50)]
    r2 = await ac.post(
        "/api/v1/hitl/batch",
        json={"ids": boundary, "action": "reject"},
        headers=HEADERS,
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["summary"] == {"ok": 0, "error": 50}
    # Sanity — every one of the 50 unknown ids surfaces as
    # ``hitl_not_pending`` (no coordinator entry).
    codes = {item["error_code"] for item in body["items"]}
    assert codes == {"hitl_not_pending"}, codes
