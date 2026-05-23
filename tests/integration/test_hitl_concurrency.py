"""Plan 7 D7.5 / Task 8 + D7.20 / Task 14 — HITL resolve race tests.

Integration tests against the real FastAPI app exercising the HITL
resolve flow's race-condition handling at multiple layers:

* :func:`test_two_resolve_same_req_race` — two concurrent
  ``POST /sessions/{sid}/hitl/{req_id}`` calls. Exactly one returns
  ``200``; the other returns ``409`` with
  ``code=hitl_already_resolved``. HITL is the *no-retry* path — the
  loser surfaces immediately rather than waiting for jitter.
* :func:`test_409_carries_first_decision` — once a request is
  resolved a follow-up resolve call returns ``409`` with a
  ``first_decision`` body fragment carrying the winning
  ``status`` / ``resolver`` / ``reason`` / ``resolved_at`` so the
  loser can render an informative error.

Task 14 additions:
* :func:`test_409_carries_first_decision_full_fields` — explicit
  field-by-field assertion that all four ``first_decision`` keys
  are populated when the DB row exists.
* :func:`test_409_error_category_field` — Task 14 added the
  ``error_category`` field for uniform machine-readable dispatch
  alongside the SDKError taxonomy on the sessions router.
* :func:`test_coordinator_race_via_db_update` — direct DB UPDATE
  short-circuits the in-process coordinator; the coordinator's
  optional store check raises :class:`HITLAlreadyResolved` so the
  router still returns 409 with a populated ``first_decision``.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
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
    """Runner that immediately completes — we don't need it for HITL tests."""
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
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/hitl.db"
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
    """Boot a fresh FastAPI app + lifespan + AsyncClient per test.

    Yields ``(client, coordinator, store)`` so each test can register
    a pending HITL row and race the resolve endpoint without going
    through a real runner-driven HITL request.
    """
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
    store: Any, coord: HITLCoordinator, *, sid: str, full_req_id: str
) -> asyncio.Task[Any]:
    """Insert a ``queued`` session + ``pending`` HITL row, register the
    coordinator future so ``resolve`` can drain it.

    Returns the ``request()`` task that's blocked on the coordinator
    future; tests should await it after the resolve race finishes.
    """
    from datetime import UTC, datetime

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
    # Yield once so the coordinator can register the entry before the
    # test races the resolve endpoint.
    await asyncio.sleep(0)
    return task


# ── tests ──────────────────────────────────────────────────────────


async def test_two_resolve_same_req_race(
    app_bundle: tuple[AsyncClient, HITLCoordinator, Any],
) -> None:
    """Exactly one of two concurrent resolves wins; the other 409s.

    HITL is the 0-retry path — the loser surfaces
    ``hitl_already_resolved`` immediately rather than blocking on a
    jitter retry.
    """
    ac, coord, store = app_bundle
    sid = "s-hitl-race"
    full_req_id = f"{sid}:r1"
    pending_task = await _seed_pending_hitl(
        store, coord, sid=sid, full_req_id=full_req_id
    )

    results = await asyncio.gather(
        ac.post(
            f"/api/v1/sessions/{sid}/hitl/r1",
            json={"decision": "accept", "resolver": "alice"},
            headers=HEADERS,
        ),
        ac.post(
            f"/api/v1/sessions/{sid}/hitl/r1",
            json={"decision": "deny", "resolver": "bob"},
            headers=HEADERS,
        ),
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 409], (
        f"expected one 200 + one 409, got {statuses}: "
        f"{[r.text for r in results]}"
    )
    loser = next(r for r in results if r.status_code == 409)
    body = loser.json()
    assert body["code"] == "hitl_already_resolved"

    # Drain the pending coordinator future so the test cleanup is clean.
    # The winning resolve set the future result so this returns
    # normally (no exception); we await the task to release its
    # resources before the fixture tears down.
    decision = await asyncio.wait_for(pending_task, timeout=0.5)
    assert decision in {"accept", "deny"}


async def test_409_carries_first_decision(
    app_bundle: tuple[AsyncClient, HITLCoordinator, Any],
) -> None:
    """The 409 body carries ``first_decision`` from the winning row."""
    ac, coord, store = app_bundle
    sid = "s-first-dec"
    full_req_id = f"{sid}:r1"
    pending_task = await _seed_pending_hitl(
        store, coord, sid=sid, full_req_id=full_req_id
    )

    # First resolve wins and persists the decision.
    r_win = await ac.post(
        f"/api/v1/sessions/{sid}/hitl/r1",
        json={
            "decision": "accept",
            "reason": "looks safe",
            "resolver": "alice",
        },
        headers=HEADERS,
    )
    assert r_win.status_code == 200, r_win.text
    decision = await asyncio.wait_for(pending_task, timeout=0.5)
    assert decision == "accept"

    # Second resolve loses; body must include the winning fragment.
    r_loss = await ac.post(
        f"/api/v1/sessions/{sid}/hitl/r1",
        json={"decision": "deny", "resolver": "bob"},
        headers=HEADERS,
    )
    assert r_loss.status_code == 409, r_loss.text
    body = r_loss.json()
    assert body["code"] == "hitl_already_resolved"
    first = body["first_decision"]
    assert first is not None
    assert first["status"] == "accept"
    # The router suffixes "|by:<resolver>" onto reason — assert the
    # original reason text survives that round-trip.
    assert "looks safe" in (first["reason"] or "")
    assert first["resolver"] == "alice"
    assert first["resolved_at"] is not None


# ── Plan 7 Task 14 (D7.20+D7.25) — extended HITL race coverage ────


async def test_409_carries_first_decision_full_fields(
    app_bundle: tuple[AsyncClient, HITLCoordinator, Any],
) -> None:
    """Field-by-field assertion that ``first_decision`` is complete.

    Plan 7 D7.20 / Task 14 — guards against silent regression where
    one of the four required keys (``status`` / ``resolver`` /
    ``reason`` / ``resolved_at``) stops being populated. The
    409 contract for HITL race losers is the foundation operators
    rely on to render "Alice already approved this at 14:32" toasts.
    """
    ac, coord, store = app_bundle
    sid = "s-full-fields"
    full_req_id = f"{sid}:r1"
    pending_task = await _seed_pending_hitl(
        store, coord, sid=sid, full_req_id=full_req_id
    )

    r_win = await ac.post(
        f"/api/v1/sessions/{sid}/hitl/r1",
        json={
            "decision": "deny",
            "reason": "looks dangerous",
            "resolver": "carol",
        },
        headers=HEADERS,
    )
    assert r_win.status_code == 200, r_win.text
    await asyncio.wait_for(pending_task, timeout=0.5)

    r_loss = await ac.post(
        f"/api/v1/sessions/{sid}/hitl/r1",
        json={"decision": "accept", "resolver": "dave"},
        headers=HEADERS,
    )
    assert r_loss.status_code == 409, r_loss.text
    body = r_loss.json()
    first = body.get("first_decision")
    assert first is not None, f"missing first_decision in body: {body}"
    # All four required keys present.
    assert set(first.keys()) >= {"status", "resolver", "reason", "resolved_at"}
    assert first["status"] == "deny"
    assert first["resolver"] == "carol"
    # Reason is suffixed with "|by:<resolver>"; assert original survives.
    assert "looks dangerous" in (first["reason"] or "")
    # resolved_at must be a non-empty isoformat string.
    assert isinstance(first["resolved_at"], str)
    assert len(first["resolved_at"]) > 0


async def test_409_error_category_field(
    app_bundle: tuple[AsyncClient, HITLCoordinator, Any],
) -> None:
    """Plan 7 D7.25 / Task 14 — 409 body carries ``error_category``.

    The HITL router uses the same ``error_category`` field name the
    sessions router emits for SDKError taxonomy mapping, so machine
    clients can dispatch uniformly on a single body key across both
    surfaces.
    """
    ac, coord, store = app_bundle
    sid = "s-err-cat"
    full_req_id = f"{sid}:r1"
    pending_task = await _seed_pending_hitl(
        store, coord, sid=sid, full_req_id=full_req_id
    )

    r_win = await ac.post(
        f"/api/v1/sessions/{sid}/hitl/r1",
        json={"decision": "accept", "resolver": "alice"},
        headers=HEADERS,
    )
    assert r_win.status_code == 200, r_win.text
    await asyncio.wait_for(pending_task, timeout=0.5)

    r_loss = await ac.post(
        f"/api/v1/sessions/{sid}/hitl/r1",
        json={"decision": "deny", "resolver": "bob"},
        headers=HEADERS,
    )
    assert r_loss.status_code == 409, r_loss.text
    body = r_loss.json()
    assert body.get("error_category") == "hitl_already_resolved", (
        f"missing/wrong error_category: {body}"
    )
    assert body.get("code") == "hitl_already_resolved"


async def test_coordinator_race_via_db_update(
    app_bundle: tuple[AsyncClient, HITLCoordinator, Any],
) -> None:
    """Plan 7 D7.20 / Task 14 — coordinator defence-in-depth via store.

    Simulate the cross-worker race the router cannot see: a direct
    DB UPDATE moves the row out of ``pending`` while the in-process
    coordinator still holds the pending future. When the API
    receives a resolve call, the coordinator's optional ``store``
    reference detects the row is no longer pending and raises
    :class:`HITLAlreadyResolved` directly. The router catches it
    and returns 409 with a fully-populated ``first_decision``.
    """
    from datetime import UTC, datetime

    ac, coord, store = app_bundle
    sid = "s-coord-race"
    full_req_id = f"{sid}:r1"
    pending_task = await _seed_pending_hitl(
        store, coord, sid=sid, full_req_id=full_req_id
    )

    # Race: directly UPDATE the row's status without touching the
    # coordinator. Emulates another worker that wrote through
    # ``upsert_hitl`` and bypassed THIS worker's coordinator.
    await store.upsert_hitl(
        id=full_req_id,
        session_id=sid,
        tool="Bash",
        args_json={"cmd": "ls"},
        status="accept",
        created_at=datetime.now(UTC),
        resolved_at=datetime.now(UTC),
        reason="raced by another worker",
        resolver="external-worker",
    )

    # Now the local API tries to resolve — coordinator sees the row
    # is no longer pending via its optional store reference.
    resp = await ac.post(
        f"/api/v1/sessions/{sid}/hitl/r1",
        json={"decision": "deny", "resolver": "local-bob"},
        headers=HEADERS,
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "hitl_already_resolved"
    assert body.get("error_category") == "hitl_already_resolved"
    first = body.get("first_decision")
    assert first is not None
    assert first["status"] == "accept"
    assert first["resolver"] == "external-worker"
    assert "raced by another worker" in (first["reason"] or "")

    # Cleanup: cancel the still-pending coordinator future so the
    # fixture teardown doesn't see a hung task.
    import contextlib

    pending_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, TimeoutError):
        await asyncio.wait_for(pending_task, timeout=0.5)
