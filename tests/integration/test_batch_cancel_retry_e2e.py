"""POST /api/v1/sessions/batch end-to-end tests — Plan 8 D8.6 / Task 9.

Black-box tests through ``httpx.AsyncClient`` against a live FastAPI
app (mirrors :mod:`tests.integration.test_role_endpoint_e2e` and
:mod:`tests.integration.test_comments_e2e`).

Covered:

  * ``test_batch_cancel_partial_success`` — three sids (two own + one
    missing) → ok=2 / error=1 (session_not_found). Validates that a
    single bad id does NOT block the rest of the batch.
  * ``test_batch_cancel_forbidden_cross_owner_non_admin`` — submitter
    Bob tries to cancel Alice's session → per-id ``forbidden_cancel``
    error_code (NOT a 403 for the whole batch).
  * ``test_batch_retry_creates_chain`` — two sids → retry → two new
    sids each with ``parent_session_id`` pointing at the original.
  * ``test_batch_max_100`` — 101 ids → 422 (pydantic ``max_length``).
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
    test can hit /batch without waiting on real work."""
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
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/batch.db"
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
    """Factory yielding (client, app) pinned to a fresh FastAPI app
    per (api_keys_raw, role_mapping_raw) tuple."""
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
            "prompt": "hi",
            "cwd": str(tmp_path),
            "plugins": {"profile": "minimal"},
            "executor": "inprocess",
            "timeout_s": 5,
            "tags": [],
        },
        "credentials": {},
    }


async def _submit(
    client: AsyncClient, tmp_path: Path, key: str
) -> str:
    r = await client.post(
        "/api/v1/sessions",
        json=_spec_body(tmp_path),
        headers={"X-API-Key": key},
    )
    assert r.status_code == 202, r.text
    return r.json()["id"]


async def test_batch_cancel_partial_success(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """Three ids: two real (owned by alice) + one nonexistent →
    ``summary={ok: 2, error: 1}``; the missing id is reported with
    ``error_code='session_not_found'``."""
    client, _app = await app_factory(
        "alice=alice-key",
        "alice=admin",
    )
    sid_a = await _submit(client, tmp_path, "alice-key")
    sid_b = await _submit(client, tmp_path, "alice-key")

    r = await client.post(
        "/api/v1/sessions/batch",
        json={
            "ids": [sid_a, sid_b, "nonexistent-sid"],
            "action": "cancel",
            "reason": "batch test",
        },
        headers={"X-API-Key": "alice-key"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] == {"ok": 2, "error": 1}
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id[sid_a]["status"] == "ok"
    assert by_id[sid_b]["status"] == "ok"
    assert by_id["nonexistent-sid"]["status"] == "error"
    assert by_id["nonexistent-sid"]["error_code"] == "session_not_found"


async def test_batch_cancel_forbidden_cross_owner_non_admin(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """Bob (submitter) tries to cancel Alice's session via batch →
    per-id ``forbidden_cancel`` error, NOT a 403 for the whole batch.
    Bob's own session in the same batch still succeeds."""
    client, _app = await app_factory(
        "alice=alice-key,bob=bob-key",
        "alice=submitter,bob=submitter",
    )
    alice_sid = await _submit(client, tmp_path, "alice-key")
    bob_sid = await _submit(client, tmp_path, "bob-key")

    r = await client.post(
        "/api/v1/sessions/batch",
        json={
            "ids": [bob_sid, alice_sid],
            "action": "cancel",
        },
        headers={"X-API-Key": "bob-key"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] == {"ok": 1, "error": 1}
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id[bob_sid]["status"] == "ok"
    assert by_id[alice_sid]["status"] == "error"
    assert by_id[alice_sid]["error_code"] == "forbidden_cancel"


async def test_batch_retry_creates_chain(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """Two sids → retry → two new sids whose ``parent_session_id``
    points at the corresponding original. The new_session_id field
    on each item is populated and the store row reflects the link."""
    client, app = await app_factory(
        "alice=alice-key",
        "alice=submitter",
    )
    sid_a = await _submit(client, tmp_path, "alice-key")
    sid_b = await _submit(client, tmp_path, "alice-key")

    r = await client.post(
        "/api/v1/sessions/batch",
        json={"ids": [sid_a, sid_b], "action": "retry"},
        headers={"X-API-Key": "alice-key"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] == {"ok": 2, "error": 0}
    by_id = {item["id"]: item for item in body["items"]}
    new_a = by_id[sid_a]["new_session_id"]
    new_b = by_id[sid_b]["new_session_id"]
    assert new_a is not None and new_b is not None
    assert new_a != sid_a and new_b != sid_b

    # Verify the parent linkage landed in the store.
    store = app.state.store
    row_a = await store.get_session(new_a)
    row_b = await store.get_session(new_b)
    assert row_a is not None and row_b is not None
    assert row_a["parent_session_id"] == sid_a
    assert row_b["parent_session_id"] == sid_b


async def test_batch_max_100(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """101 ids → 422 from pydantic ``max_length`` validation, before
    the router runs (so no partial side-effects). Sanity: 100 ids
    without real sessions all surface as ``session_not_found``
    rather than triggering the 422 path."""
    del tmp_path  # not needed for this validation test
    client, _app = await app_factory(
        "alice=alice-key",
        "alice=admin",
    )
    too_many = [f"sid-{i}" for i in range(101)]
    r = await client.post(
        "/api/v1/sessions/batch",
        json={"ids": too_many, "action": "cancel"},
        headers={"X-API-Key": "alice-key"},
    )
    assert r.status_code == 422, r.text

    # Sanity — exactly 100 is accepted (and all 100 unknown ids
    # surface as session_not_found, not as a validation error).
    boundary = [f"sid-{i}" for i in range(100)]
    r2 = await client.post(
        "/api/v1/sessions/batch",
        json={"ids": boundary, "action": "cancel"},
        headers={"X-API-Key": "alice-key"},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["summary"] == {"ok": 0, "error": 100}
