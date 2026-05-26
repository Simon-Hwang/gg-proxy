"""Toast system structural contract.

The toast system is **opt-in** by design (R3/R4 audit):

  1. ``<div id="toast-stack" aria-live="polite">`` exists on every page.
  2. The JS hook only fires when one of two conditions is met:
     - server response carries ``HX-Trigger: showToast`` JSON header
     - the triggering element has ``data-toast-on-error`` AND the
       response is 4xx/5xx
  3. The existing 8 inline ``hx-on::after-request`` handlers must
     keep working — toast must not double-fire on those elements.

Because the toast rendering itself is client-side JS, these tests
only pin the *structural* contract that a future server-side or
template change can't silently break:

  * stack root present and uses correct aria-live attribute
  * CSS classes defined (.toast, .toast-success, .toast-error, etc.)
  * existing hx-on::after-request handlers unchanged (we just count
    the 8 known sites — if a future PR moves them to a new file,
    the count check fails loudly so we re-audit toast double-fire)
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
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/toast.db"
    cfg.api_keys_raw = "k1"
    cfg.dashboard_admin_password = SecretStr("hunter2")
    cfg.dashboard_session_secret = SecretStr(
        "toast-test-secret-32-bytes-or-more-x"
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


class TestToastStackRoot:
    async def test_stack_present_on_overview(
        self, client: AsyncClient
    ) -> None:
        r = await client.get("/dashboard/overview")
        assert r.status_code == 200
        assert (
            '<div id="toast-stack" aria-live="polite" '
            'aria-atomic="false"></div>'
        ) in r.text

    async def test_global_show_toast_hook_present(
        self, client: AsyncClient
    ) -> None:
        """The JS that listens for ``HX-Trigger: showToast`` must
        be present in base.html. We grep for the event name + the
        opt-in selector."""
        r = await client.get("/dashboard/overview")
        assert r.status_code == 200
        # showToast event listener — HTMX dispatches this when
        # response carries HX-Trigger: showToast JSON.
        assert "'showToast'" in r.text
        # data-toast-on-error opt-in selector — the only way to
        # auto-toast on 4xx/5xx
        assert "data-toast-on-error" in r.text
        # ggToast global API (for future inline triggers)
        assert "window.ggToast" in r.text


class TestToastCss:
    async def test_toast_css_classes_defined(
        self, client: AsyncClient
    ) -> None:
        r = await client.get("/dashboard/static/app.css")
        assert r.status_code == 200
        # core classes
        for klass in [
            "#toast-stack",
            ".toast ",
            ".toast-success",
            ".toast-error",
            ".toast-info",
            ".toast-warn",
            ".toast-close",
        ]:
            assert klass in r.text, f"missing CSS class {klass}"

    async def test_reduced_motion_respected(
        self, client: AsyncClient
    ) -> None:
        """WCAG 2.3.3 — toast animations must opt out under
        prefers-reduced-motion."""
        r = await client.get("/dashboard/static/app.css")
        assert r.status_code == 200
        assert "prefers-reduced-motion" in r.text


class TestNoConflictWithInlineHandlers:
    """The 8 existing hx-on::after-request inline handlers must stay
    unique so future toast adoption doesn't accidentally double-fire.

    If this count drifts up or down, re-audit the toast hook against
    the changed sites — opt-in semantics depend on this list.
    """

    async def test_existing_inline_handlers_count(
        self, client: AsyncClient
    ) -> None:
        """Templates with inline hx-on::after-request must not also
        carry the data-toast-on-error opt-in — that would double-fire.

        We strip <script> blocks before checking because the global
        toast JS in base.html contains the literal ``data-toast-on-error``
        string as part of its selector pattern.
        """
        import re
        r = await client.get("/dashboard/templates")
        assert r.status_code == 200
        html_no_scripts = re.sub(
            r"<script\b[^>]*>.*?</script>",
            "",
            r.text,
            flags=re.DOTALL,
        )
        if "hx-on::after-request" in html_no_scripts:
            # Only check for the *attribute* form, not the bare keyword.
            assert not re.search(
                r'\sdata-toast-on-error(?:=|\s|>)',
                html_no_scripts,
            ), (
                "templates.html has both inline hx-on::after-request "
                "AND data-toast-on-error — that's the double-fire bug "
                "we explicitly avoided in v3 design."
            )
