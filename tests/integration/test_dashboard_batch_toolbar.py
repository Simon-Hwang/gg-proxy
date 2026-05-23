"""Plan 8 Task 10 / D8.6 — dashboard batch toolbar render + dispatch.

Covers the dashboard surface added by Task 10:

* ``test_kanban_includes_batch_toolbar_and_script`` — the toolbar
  partial (``_batch_toolbar.html``) renders inside the kanban page
  chrome, and the ``batch_toolbar.js`` ``<script>`` tag is wired so
  the browser loads the selection / dispatch logic.
* ``test_session_cards_include_bulk_select_checkbox`` — each kanban
  card carries a ``<input class="bulk-select" data-session-id=...>``
  so the JS can target it. Two seeded sessions → ≥2 checkboxes.
* ``test_batch_endpoint_reachable_via_dashboard_cookie`` — the
  ``DashboardCookieMiddleware`` injects the synthetic ``X-API-Key``
  for ``/api/v1/*`` mutations, so the browser doesn't need to send
  an explicit api key header. We verify the cookie-only ``POST
  /api/v1/sessions/batch`` lands on the router and returns the
  per-id ``items`` + ``summary`` envelope from Task 9.

The dispatch path (button click → ``fetch`` → result panel + reload
trigger) is JS-only; e2e-testing it would require Playwright /
Selenium which Task 10 intentionally avoids (no new deps). The
``test_batch_endpoint_reachable_via_dashboard_cookie`` check is the
integration-level proxy: if the endpoint is reachable via the
cookie, the JS dispatch will work too.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import bcrypt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.store import SessionRepository, create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


def _bcrypt_hash(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")


def _make_cfg(tmp_path: Path) -> Config:
    """Config tuned for cookie-only dashboard tests.

    - ``api_keys_raw`` empty so the *only* api key in play is the
      synthetic one the dashboard cookie middleware mints at
      lifespan startup.
    - ``dashboard_users_raw`` carries the bcrypt-hashed admin so
      :func:`_derive_dashboard_internal_keys` mints an internal API
      key for ``admin`` (the legacy ``dashboard_admin_password``
      login skips that step, leaving ``/api/v1/*`` calls unsigned).
    - ``role_mapping_raw`` empty — the autouse conftest fixture
      ``_test_role_mapping_default`` then grants ``admin`` to any
      authenticated request so the ``require_role('submitter')``
      guard on /api/v1/sessions/batch is satisfied via the
      cookie identity.
    - ``kanban_default_page_size`` large enough that both seeded
      sessions land on page 1 (default 50 is fine; we set it
      explicitly to insulate the test from future default tweaks).
    """
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/batch-toolbar.db"
    cfg.api_keys_raw = ""
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.dashboard_users_raw = f"admin={_bcrypt_hash('hunter2')}"
    cfg.dashboard_session_secret = SecretStr(
        "a-test-secret-32-bytes-or-longer-xxxx"
    )
    cfg.public_base_url = "http://t"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    cfg.kanban_default_page_size = 50
    return cfg


async def _seed_sessions(store: SessionRepository, count: int) -> list[str]:
    """Insert ``count`` session rows directly through the store.

    Bypasses ``/api/v1/sessions`` so the test doesn't need to wire
    an in-process executor — the kanban renderer only needs the row
    to project a ``SessionSummary``, and the batch endpoint only
    needs the row to exist for the per-id lookup.
    """
    ids: list[str] = []
    for i in range(count):
        sid = f"sid-toolbar-{i}"
        await store.create_session(
            id=sid,
            spec_json={"prompt": f"seed-{i}"},
            trace_id=None,
            backend="inprocess",
            tags=(),
            owner="dashboard-admin",
        )
        ids.append(sid)
    return ids


@pytest_asyncio.fixture
async def client_and_store(tmp_path: Path):
    """Yield a dashboard-logged-in ``AsyncClient`` + store handle.

    The fixture follows the same recipe as
    ``test_dashboard_kanban.py``: spin a real FastAPI app via
    ``ASGITransport``, run lifespan so the dashboard cookie
    middleware mints its in-memory key, and POST to
    ``/dashboard/login`` so subsequent requests carry a valid
    Starlette session cookie.
    """
    cfg = _make_cfg(tmp_path)
    app = create_app(cfg)
    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
    ) as ac, app.router.lifespan_context(app):
        r = await ac.post(
            "/dashboard/login",
            data={"username": "admin", "password": "hunter2"},
        )
        assert r.status_code == 303, r.text
        yield ac, app.state.store


async def test_kanban_includes_batch_toolbar_and_script(
    client_and_store: tuple[AsyncClient, Any],
) -> None:
    """Kanban page chrome carries the toolbar fragment + JS script.

    Asserts (purely render-time):

    * ``id="batch-toolbar"`` from ``_batch_toolbar.html`` is present.
    * ``batch_toolbar.js`` is referenced as a ``<script src=...>``
      so the browser loads the selection logic.
    * The HTMX ``kanban:reload from:body`` trigger is wired so the
      JS reload event actually refreshes the board.
    """
    client, _store = client_and_store
    r = await client.get("/dashboard/kanban")
    assert r.status_code == 200, r.text
    body = r.text
    assert 'id="batch-toolbar"' in body
    assert "batch_toolbar.js" in body
    assert "kanban:reload from:body" in body
    # Buttons start disabled — JS flips them on the first selection.
    assert 'id="btn-batch-cancel"' in body
    assert 'id="btn-batch-retry"' in body
    assert 'id="btn-batch-clear"' in body


async def test_session_cards_include_bulk_select_checkbox(
    client_and_store: tuple[AsyncClient, Any],
) -> None:
    """Each seeded card emits a ``.bulk-select`` checkbox + data-id.

    Two sessions seeded → both ids visible on the rendered card +
    at least two ``class="bulk-select"`` checkbox occurrences (one
    per card).
    """
    client, store = client_and_store
    sids = await _seed_sessions(store, 2)

    r = await client.get("/dashboard/kanban/board")
    assert r.status_code == 200, r.text
    body = r.text
    for sid in sids:
        assert f'data-session-id="{sid}"' in body
    # One checkbox per card → at least two.
    assert body.count('class="bulk-select"') >= 2
    # Each card's link target should still resolve to the detail page
    # (regression check: the checkbox sits outside the anchor so
    # navigation isn't broken).
    for sid in sids:
        assert f'href="/dashboard/sessions/{sid}"' in body


async def test_batch_endpoint_reachable_via_dashboard_cookie(
    client_and_store: tuple[AsyncClient, Any],
) -> None:
    """Cookie-only ``POST /api/v1/sessions/batch`` returns 200 + envelope.

    The dashboard cookie middleware injects the synthetic
    ``X-API-Key`` for ``/api/v1/*`` requests, so no explicit api key
    header is needed on the call. We verify:

    * 200 status (router runs to completion — no auth rejection).
    * Response carries the ``items`` + ``summary`` envelope from
      ``BatchSessionResponse`` (Task 9 contract).
    * ``len(items) == 2`` — one entry per submitted id (per-id
      success / failure is not asserted; the test intent is that the
      cookie path reaches the router, not the cancel state machine).
    """
    client, store = client_and_store
    sids = await _seed_sessions(store, 2)

    r = await client.post(
        "/api/v1/sessions/batch",
        json={
            "ids": sids,
            "action": "cancel",
            "reason": "dashboard_batch_cancel",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "items" in data
    assert "summary" in data
    assert len(data["items"]) == 2
    by_id = {item["id"]: item for item in data["items"]}
    for sid in sids:
        assert sid in by_id
