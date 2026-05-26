"""Chart polish — C1 status donut + C2 responsive tokens chart.

Pinned contracts:

  * Overview tokens chart canvas lives inside a `.chart-container`
    so Chart.js `responsive:true + maintainAspectRatio:false` fills
    the parent and stops overflowing < 1024 px viewports.
  * Status mix renders BOTH the legend cells (accessible source of
    truth, parsed by Chart.js for the donut data) AND the canvas
    (visual). Canvas carries `role="img"` and `aria-label`.
  * Donut center shows the total session count derived from
    status_mix (server-side) so it's accurate before JS runs.
  * Chart.js config switched to responsive:true; the literal
    `responsive: false` from the old fixed-size config must NOT
    appear in overview.html anymore.
  * Reduced-motion users get no animation (delegated to Chart.js
    `animation:false` is out of scope here — covered by
    prefers-reduced-motion media query on toast/cmdk; chart
    animations are short enough to be acceptable).
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
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/charts.db"
    cfg.api_keys_raw = "k1"
    cfg.dashboard_admin_password = SecretStr("hunter2")
    cfg.dashboard_session_secret = SecretStr(
        "charts-test-secret-32-bytes-min-x"
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
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as ac, app.router.lifespan_context(app):
        await ac.post(
            "/dashboard/login",
            data={"username": "admin", "password": "hunter2"},
        )
        yield ac


class TestTokensChartResponsive:
    async def test_canvas_wrapped_in_chart_container(
        self, client: AsyncClient
    ) -> None:
        r = await client.get("/dashboard/overview")
        assert r.status_code == 200
        # canvas no longer carries fixed width/height (C2 regression):
        assert '<canvas id="overview-chart" width="900" height="240">' not in r.text
        # ...and lives inside a .chart-container parent:
        assert '<div class="chart-container">' in r.text
        assert '<canvas id="overview-chart"></canvas>' in r.text

    async def test_chart_js_responsive_config(
        self, client: AsyncClient
    ) -> None:
        r = await client.get("/dashboard/overview")
        assert r.status_code == 200
        # The old fixed-size config used responsive:false — must
        # not survive the refactor.
        assert "responsive: false" not in r.text
        assert "responsive: true" in r.text
        assert "maintainAspectRatio: false" in r.text

    async def test_chart_container_css_defined(
        self, client: AsyncClient
    ) -> None:
        r = await client.get("/dashboard/static/app.css")
        assert r.status_code == 200
        assert ".chart-container {" in r.text
        # The donut variant has a tighter max-width
        assert ".chart-container--donut {" in r.text


class TestStatusMixDonut:
    async def test_donut_canvas_present_and_a11y(
        self, client: AsyncClient
    ) -> None:
        r = await client.get("/dashboard/overview")
        assert r.status_code == 200
        # The donut canvas must exist with role=img + aria-label
        # because Chart.js draws to non-textual <canvas>; SR users
        # rely on the explicit role/label + the legend cells.
        assert 'id="status-mix-chart"' in r.text
        assert 'role="img"' in r.text
        assert 'aria-label="Session status distribution donut chart"' in r.text

    async def test_legend_cells_still_render(
        self, client: AsyncClient
    ) -> None:
        """The legend cells (status-cell dot-<key>) are BOTH the
        accessible source of truth AND the data source the donut
        JS reads from. Removing them would break a11y AND the chart."""
        r = await client.get("/dashboard/overview")
        assert r.status_code == 200
        for key in ("queued", "running", "paused", "completed", "failed", "cancelled"):
            assert f'class="status-cell dot-{key}"' in r.text, (
                f"missing legend cell for status={key} — donut JS "
                "reads from these; removing them breaks the chart "
                "AND the screen-reader fallback."
            )

    async def test_donut_center_shows_total(
        self, client: AsyncClient
    ) -> None:
        """Center text must be visible before JS runs (server-side
        rendering). An empty database renders ``0 total``."""
        r = await client.get("/dashboard/overview")
        assert r.status_code == 200
        assert 'class="donut-center"' in r.text
        assert 'class="donut-center-num"' in r.text
        # default empty store → 0 sessions
        assert '<span class="donut-center-num">0</span>' in r.text
        assert '<span class="donut-center-label">total</span>' in r.text

    async def test_canvas_legend_disabled_to_avoid_duplication(
        self, client: AsyncClient
    ) -> None:
        """The static cells already serve as the legend — Chart.js's
        own legend must be off to avoid visual duplication."""
        r = await client.get("/dashboard/overview")
        assert r.status_code == 200
        assert "legend: { display: false }" in r.text

    async def test_cancelled_cell_has_dot_color(
        self, client: AsyncClient
    ) -> None:
        """The status-mix legend includes 'cancelled' but the dot
        color wasn't originally defined → invisible dot. Verify the
        fix is in CSS."""
        r = await client.get("/dashboard/static/app.css")
        assert r.status_code == 200
        assert ".status-cell.dot-cancelled .dot" in r.text
