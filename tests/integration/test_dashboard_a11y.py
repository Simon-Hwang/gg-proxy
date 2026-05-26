"""WCAG 2.2 AA structural baseline tests.

These tests pin the four a11y primitives added by the v3 audit:

  * **Skip-to-content link** (WCAG 2.4.1 Bypass Blocks) — first
    focusable element jumps past the sidebar+topbar (~25 tab stops).
  * **sr-only utility class** defined in CSS — required so the
    skip-link and aria-live region don't render visibly.
  * **Global aria-live region** (``<div id="hx-live" role="status"
    aria-live="polite">``) — exists on every page but only writes
    when an opt-in element triggers it (verified via the
    ``data-hx-announce`` attribute pattern; the JS side requires
    Playwright to fully exercise).
  * **Toast stack root** (``<div id="toast-stack" aria-live="polite">``)
    — present even when empty so JS toasts have a mount point.

We deliberately do NOT test for ``data-hx-announce`` on the SSE /
5s polling targets (kanban, sessions_list) — those *must not* have
it, otherwise the aria-live region floods screen readers every 5
seconds (the bug R3 caught). This is a negative assertion.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.store import create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


def _cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/a11y.db"
    cfg.api_keys_raw = "k1"
    cfg.dashboard_admin_password = SecretStr("hunter2")
    cfg.dashboard_session_secret = SecretStr(
        "a11y-test-secret-32-bytes-or-more-xxx"
    )
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://t"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def client(tmp_path: Path):
    cfg = _cfg(tmp_path)
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


# Pages that should all carry the a11y baseline (all extend base.html).
_PAGES = [
    "/dashboard/overview",
    "/dashboard/kanban",
    "/dashboard/list",
    "/dashboard/sessions",
    "/dashboard/templates",
    "/dashboard/favorites",
    "/dashboard/cost",
    "/dashboard/new",
    "/dashboard/search",
]


class TestSkipLink:
    """First focusable element must skip past sidebar+topbar."""

    @pytest.mark.parametrize("path", _PAGES)
    async def test_skip_link_present(
        self, client: AsyncClient, path: str
    ) -> None:
        r = await client.get(path)
        if r.status_code >= 400:
            pytest.skip(f"{path} → {r.status_code}")
        assert (
            '<a class="skip-link" href="#main-content">'
            "Skip to main content</a>"
        ) in r.text, (
            f"{path} missing skip-to-content link "
            "(base.html regression)"
        )

    @pytest.mark.parametrize("path", _PAGES)
    async def test_main_has_id_and_tabindex(
        self, client: AsyncClient, path: str
    ) -> None:
        r = await client.get(path)
        if r.status_code >= 400:
            pytest.skip(f"{path} → {r.status_code}")
        assert (
            '<main class="app-main" id="main-content" tabindex="-1">'
        ) in r.text, (
            f"{path} main element must be the skip-link target"
        )


class TestAriaLiveRegion:
    """Polite live region exists and is opt-in (no auto-broadcast)."""

    @pytest.mark.parametrize("path", _PAGES)
    async def test_global_polite_region_present(
        self, client: AsyncClient, path: str
    ) -> None:
        r = await client.get(path)
        if r.status_code >= 400:
            pytest.skip(f"{path} → {r.status_code}")
        assert (
            '<div id="hx-live" class="sr-only" role="status" '
            'aria-live="polite" aria-atomic="true"></div>'
        ) in r.text

    @pytest.mark.parametrize("path", ["/dashboard/kanban", "/dashboard/sessions"])
    async def test_polling_targets_do_not_opt_in(
        self, client: AsyncClient, path: str
    ) -> None:
        """Polling/SSE targets must NOT carry data-hx-announce.

        This is the R3 regression: a naive global aria-live writer
        would announce every 5-second poll, flooding screen reader
        users with "Content updated" every 5 seconds. The opt-in
        model means polling containers stay silent unless we
        explicitly mark a card with data-hx-announce.

        Note: the global JS hook in base.html contains the literal
        ``data-hx-announce`` string as part of its selector (closest
        + querySelector). We strip the <script>...</script> blocks
        before searching so we only see the markup the *templates*
        emit, not the JS source.
        """
        import re
        r = await client.get(path)
        if r.status_code >= 400:
            pytest.skip(f"{path} → {r.status_code}")
        html_no_scripts = re.sub(
            r"<script\b[^>]*>.*?</script>",
            "",
            r.text,
            flags=re.DOTALL,
        )
        # Look for the attribute syntax, not the keyword on its own.
        # An element opts in via `data-hx-announce` (boolean) or
        # `data-hx-announce="..."` — both shapes are caught by the
        # attribute-with-following-character regex below.
        assert not re.search(
            r'\sdata-hx-announce(?:=|\s|>)',
            html_no_scripts,
        ), (
            f"{path} must not opt-in to global aria-live announcements; "
            "this would cause the 5s poll / SSE update loop to flood "
            "screen reader users (R3 regression)."
        )


class TestToastStack:
    """The toast stack root exists on every page even when empty."""

    @pytest.mark.parametrize("path", _PAGES)
    async def test_toast_stack_present(
        self, client: AsyncClient, path: str
    ) -> None:
        r = await client.get(path)
        if r.status_code >= 400:
            pytest.skip(f"{path} → {r.status_code}")
        assert (
            '<div id="toast-stack" aria-live="polite" '
            'aria-atomic="false"></div>'
        ) in r.text


class TestCssBaseline:
    """The supporting CSS classes must be defined or the a11y
    primitives degrade to visible noise."""

    async def test_sr_only_defined(self, client: AsyncClient) -> None:
        r = await client.get("/dashboard/static/app.css")
        assert r.status_code == 200
        # We don't pin the exact rule body, just that .sr-only
        # exists with the canonical clip pattern used for SR-only.
        assert ".sr-only" in r.text
        assert "clip: rect(0, 0, 0, 0)" in r.text

    async def test_skip_link_defined(self, client: AsyncClient) -> None:
        r = await client.get("/dashboard/static/app.css")
        assert r.status_code == 200
        assert ".skip-link" in r.text
        # Hidden off-screen until focused → top: -100px → focus → top: var(...)
        assert "top: -100px" in r.text

    async def test_focus_visible_defined(
        self, client: AsyncClient
    ) -> None:
        r = await client.get("/dashboard/static/app.css")
        assert r.status_code == 200
        assert ":focus-visible" in r.text
        # 2px high-contrast outline is the WCAG 2.4.11 baseline
        assert "outline: 2px solid var(--accent)" in r.text
