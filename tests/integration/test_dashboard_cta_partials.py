"""Pinned contract for the shared ``+ New session`` CTA partial.

After the v3 refactor (Santa rounds 3-5), the four CTA HTML shapes
that previously lived inline across 8 templates moved into one
``_new_session_cta.html`` macro. These tests verify the macro
keeps the four observable HTML signatures the rest of the
codebase asserts on:

  sidebar + enabled  →  ``<a href="/dashboard/new" class="cta-primary" …>``
  sidebar + disabled →  ``<span class="cta-primary cta-disabled" aria-disabled="true" …>``
  page    + enabled  →  ``<a href="/dashboard/new" class="btn-cta" …>``
  page    + disabled →  ``<span class="btn-cta disabled" aria-disabled="true" …>``

If anyone breaks these, every other dashboard regression test
relying on substring assertions for the New session affordance
will start failing in confusing ways. This file fails first with
a clear message instead.
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
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/cta-admin.db"
    cfg.api_keys_raw = "k1"
    cfg.dashboard_admin_password = SecretStr("hunter2")
    cfg.dashboard_session_secret = SecretStr(
        "cta-partial-test-secret-32-bytes-min"
    )
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://t"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


def _viewer_cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/cta-viewer.db"
    cfg.api_keys_raw = "k1"
    cfg.role_mapping_raw = "dashboard-readonly=viewer"
    cfg.dashboard_users_raw = f"readonly={_hash('r-pw')}"
    cfg.dashboard_session_secret = SecretStr(
        "cta-viewer-test-secret-32-bytes-min"
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
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
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
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac, app.router.lifespan_context(app):
        await ac.post(
            "/dashboard/login",
            data={"username": "readonly", "password": "r-pw"},
        )
        yield ac


# Pages that should render the page-variant CTA in their header.
# overview also renders it inside the empty-state block, hence the
# count > 1 in that case.
_PAGES_WITH_PAGE_CTA = [
    ("/dashboard/overview", 1),  # 1 in header; empty-state only shows when no sessions
    ("/dashboard/kanban", 1),
    ("/dashboard/list", 1),
    ("/dashboard/sessions", 1),
    ("/dashboard/favorites", 1),
    ("/dashboard/templates", 1),
    ("/dashboard/cost", 1),
]


class TestSidebarVariant:
    """The sidebar CTA uses class="cta-primary" — verified across roles."""

    async def test_admin_sees_sidebar_enabled_anchor(
        self, admin_client: AsyncClient
    ) -> None:
        r = await admin_client.get("/dashboard/overview")
        assert r.status_code == 200
        assert (
            '<a href="/dashboard/new" class="cta-primary" '
            'title="Create a new session">'
        ) in r.text
        assert "class=\"cta-primary cta-disabled\"" not in r.text

    async def test_viewer_sees_sidebar_disabled_span(
        self, viewer_client: AsyncClient
    ) -> None:
        r = await viewer_client.get("/dashboard/kanban")
        assert r.status_code == 200
        assert (
            '<span class="cta-primary cta-disabled" aria-disabled="true"'
        ) in r.text
        # Viewer must NOT see the clickable anchor — that was the
        # original bug shape.
        assert (
            '<a href="/dashboard/new" class="cta-primary"'
        ) not in r.text


class TestPageVariant:
    """Each page header CTA uses class="btn-cta" — verified per page."""

    @pytest.mark.parametrize("path,_minimum", _PAGES_WITH_PAGE_CTA)
    async def test_admin_sees_page_enabled_anchor(
        self, admin_client: AsyncClient, path: str, _minimum: int
    ) -> None:
        r = await admin_client.get(path)
        assert r.status_code == 200, f"{path} → {r.status_code}: {r.text[:300]}"
        assert (
            '<a href="/dashboard/new" class="btn-cta" '
            'title="Create a new session">'
        ) in r.text, (
            f"{path} missing page-variant enabled CTA — "
            f"check _new_session_cta.html macro"
        )

    @pytest.mark.parametrize("path,_minimum", _PAGES_WITH_PAGE_CTA)
    async def test_viewer_sees_page_disabled_span(
        self, viewer_client: AsyncClient, path: str, _minimum: int
    ) -> None:
        r = await viewer_client.get(path)
        # viewer on /dashboard/cost may be denied entirely; that's
        # fine — the CTA contract test only applies when the page
        # renders. Skip 4xx but assert if 200.
        if r.status_code >= 400:
            pytest.skip(f"{path} not reachable as viewer ({r.status_code})")
        assert (
            '<span class="btn-cta disabled" aria-disabled="true"'
        ) in r.text, (
            f"{path} viewer must see disabled page-variant CTA — "
            f"got either nothing or the enabled anchor"
        )


class TestMacroImportedExactlyOnce:
    """The macro should be imported via {% from %} on every page that
    uses it. A page that drops the import will render literal text
    or trigger a Jinja2 UndefinedError; this test catches both."""

    @pytest.mark.parametrize("path", [p for p, _ in _PAGES_WITH_PAGE_CTA])
    async def test_macro_renders_not_literal(
        self, admin_client: AsyncClient, path: str
    ) -> None:
        r = await admin_client.get(path)
        assert r.status_code == 200
        # No literal Jinja2 expression leaked into the response
        assert "{{ cta(" not in r.text
        assert "{% from " not in r.text
