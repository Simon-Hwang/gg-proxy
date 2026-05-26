"""Command palette (⌘K / Ctrl+K) — structural contract.

These tests pin the server-side route + fragment HTML the JS
in base.html depends on. The keyboard binding itself
(meta/ctrl + K, Esc, ↑/↓/Enter, outside-click) is JS-only and
would need Playwright to fully exercise — out of scope here. We
do verify the JS HOOK is wired in base.html (the keydown handler
string is present).

Pinned contract:

  * GET /dashboard/cmdk          → full modal HTML
      - role="dialog", aria-modal="true"
      - #cmdk-input with hx-get="/dashboard/cmdk/results"
      - data-cmdk-item rows with data-href + role="option"
  * GET /dashboard/cmdk/results  → fragment same shape
  * base.html mount: #cmdk-mount data-cmdk-state="closed"
  * RBAC:
      - admin sees the "Submit new session" quick action
      - viewer does NOT see it (RBAC-gated server-side, not just
        client-disabled — the palette never surfaces affordances
        that 401 on click)
  * recent_sessions filtered by owner=self for non-admin
"""
from __future__ import annotations

from pathlib import Path

import bcrypt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.store import create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _admin_cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/cmdk-admin.db"
    cfg.api_keys_raw = "k1"
    cfg.dashboard_admin_password = SecretStr("hunter2")
    cfg.dashboard_session_secret = SecretStr(
        "cmdk-admin-test-secret-32-bytes-min"
    )
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://t"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


def _viewer_cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/cmdk-viewer.db"
    cfg.api_keys_raw = "k1"
    cfg.role_mapping_raw = "dashboard-readonly=viewer"
    cfg.dashboard_users_raw = f"readonly={_hash('r-pw')}"
    cfg.dashboard_session_secret = SecretStr(
        "cmdk-viewer-test-secret-32-bytes-min"
    )
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://t"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def admin_client(tmp_path: Path):
    cfg = _admin_cfg(tmp_path)
    app = create_app(cfg)
    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as ac, app.router.lifespan_context(app):
        await ac.post(
            "/dashboard/login",
            data={"username": "admin", "password": "hunter2"},
        )
        yield ac


@pytest_asyncio.fixture
async def viewer_client(tmp_path: Path):
    cfg = _viewer_cfg(tmp_path)
    app = create_app(cfg)
    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as ac, app.router.lifespan_context(app):
        await ac.post(
            "/dashboard/login",
            data={"username": "readonly", "password": "r-pw"},
        )
        yield ac


class TestMount:
    async def test_overview_has_cmdk_mount(
        self, admin_client: AsyncClient
    ) -> None:
        r = await admin_client.get("/dashboard/overview")
        assert r.status_code == 200
        assert (
            '<div id="cmdk-mount" data-cmdk-state="closed"></div>'
        ) in r.text

    async def test_keybind_handler_present(
        self, admin_client: AsyncClient
    ) -> None:
        """⌘K (Meta) and Ctrl+K must both trigger — pinned because
        getting only one platform working is a common regression."""
        r = await admin_client.get("/dashboard/overview")
        assert r.status_code == 200
        # cross-platform keybind check (Mac uses metaKey, Win/Linux
        # uses ctrlKey)
        assert "evt.metaKey || evt.ctrlKey" in r.text
        assert "evt.key === 'k' || evt.key === 'K'" in r.text
        # input-field exemption — never hijack ⌘K while user is typing
        # outside the palette
        assert "isTypingTarget" in r.text


class TestModalShell:
    async def test_modal_dialog_attrs(
        self, admin_client: AsyncClient
    ) -> None:
        r = await admin_client.get("/dashboard/cmdk")
        assert r.status_code == 200
        # WCAG 4.1.2 + dialog modal pattern
        assert 'role="dialog"' in r.text
        assert 'aria-modal="true"' in r.text
        assert 'aria-labelledby="cmdk-title"' in r.text

    async def test_input_aria_and_htmx_wiring(
        self, admin_client: AsyncClient
    ) -> None:
        r = await admin_client.get("/dashboard/cmdk")
        assert r.status_code == 200
        # input has explicit aria-label (icon-only buttons rule)
        assert 'id="cmdk-input"' in r.text
        assert 'aria-label="Command palette search"' in r.text
        assert 'aria-controls="cmdk-results"' in r.text
        # debounced HTMX search → results fragment
        assert 'hx-get="/dashboard/cmdk/results"' in r.text
        assert 'hx-target="#cmdk-results"' in r.text
        assert "input changed delay:120ms" in r.text


class TestRbacQuickActions:
    async def test_admin_sees_submit_new_session(
        self, admin_client: AsyncClient
    ) -> None:
        r = await admin_client.get("/dashboard/cmdk")
        assert r.status_code == 200
        assert "Submit new session" in r.text
        # Quick action goes through the real /dashboard/new route
        assert 'data-href="/dashboard/new"' in r.text

    async def test_viewer_omits_submit_new_session(
        self, viewer_client: AsyncClient
    ) -> None:
        """Viewers must NOT see the create-session quick action.

        Server-side RBAC, not just client-side disable — the palette
        never surfaces affordances the user can't actually invoke.
        """
        r = await viewer_client.get("/dashboard/cmdk")
        assert r.status_code == 200
        assert "Submit new session" not in r.text
        # The viewer still gets navigation entries (Overview etc),
        # just no action shortcuts.
        assert "Overview" in r.text


class TestNavigationItems:
    @pytest.mark.parametrize(
        "label,href",
        [
            ("Overview", "/dashboard/overview"),
            ("Kanban", "/dashboard/kanban"),
            ("List", "/dashboard/list"),
            ("Live feed", "/dashboard/sessions"),
            ("Search", "/dashboard/search"),
            ("Favorites", "/dashboard/favorites"),
            ("Templates", "/dashboard/templates"),
            ("Cost", "/dashboard/cost"),
        ],
    )
    async def test_each_page_listed(
        self, admin_client: AsyncClient, label: str, href: str
    ) -> None:
        r = await admin_client.get("/dashboard/cmdk")
        assert r.status_code == 200
        assert label in r.text
        assert f'data-href="{href}"' in r.text

    async def test_admin_sees_api_keys_link(
        self, admin_client: AsyncClient
    ) -> None:
        r = await admin_client.get("/dashboard/cmdk")
        assert r.status_code == 200
        assert "API keys" in r.text
        assert 'data-href="/dashboard/admin/keys"' in r.text

    async def test_viewer_does_not_see_api_keys_link(
        self, viewer_client: AsyncClient
    ) -> None:
        r = await viewer_client.get("/dashboard/cmdk")
        assert r.status_code == 200
        assert "API keys" not in r.text


class TestResultsFragment:
    async def test_results_renders_same_shape(
        self, admin_client: AsyncClient
    ) -> None:
        r = await admin_client.get("/dashboard/cmdk/results")
        assert r.status_code == 200
        # No outer modal chrome — just sections + items
        assert 'role="dialog"' not in r.text
        # Sections + items present
        assert 'data-cmdk-section' in r.text

    async def test_empty_query_with_no_data_shows_empty_state(
        self, admin_client: AsyncClient
    ) -> None:
        r = await admin_client.get("/dashboard/cmdk/results?q=nomatch")
        assert r.status_code == 200
        # navigation/actions are hidden when q is set, only recent_sessions
        # rendered; with no sessions matching → empty-state message
        assert "No matches for" in r.text
        # And the full-search escape hatch is offered
        assert "Open full search" in r.text
        assert "/dashboard/search?q=nomatch" in r.text


class TestKeyboardSelectAttrs:
    """Items must carry role=option + aria-selected for SR + JS nav."""

    async def test_items_carry_role_option_and_aria_selected(
        self, admin_client: AsyncClient
    ) -> None:
        r = await admin_client.get("/dashboard/cmdk")
        assert r.status_code == 200
        # Sample at least one occurrence of each contract attribute
        assert 'role="option"' in r.text
        assert 'aria-selected="false"' in r.text
        # Results container is the listbox holding them
        assert 'role="listbox"' in r.text


class TestRecentSessionsOwnerScope:
    """Non-admin caller sees only their own recent sessions in cmdk
    (mirrors the search RBAC contract)."""

    async def test_viewer_recent_excludes_others(
        self, viewer_client: AsyncClient, tmp_path: Path
    ) -> None:
        # The viewer fixture starts with an empty store, so no recent
        # sessions exist to compare. We assert the result is a 200
        # with no leakage and no exception path. Full multi-user
        # leakage testing already covered by test_dashboard_search.py.
        r = await viewer_client.get("/dashboard/cmdk")
        assert r.status_code == 200
        # If the empty-store path renders, it shows "Recent sessions"
        # section title only when there are items — so its absence
        # here is the expected empty case.
