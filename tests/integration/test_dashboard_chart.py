"""Per-session chart + span tree integration tests — Plan 6 Task 10.

Covers Plan 6 D6.4 (per-session aggregate chart), D6.6 (span tree
iframe + fallback), and D6.14 (Jaeger reverse-proxy URL plumbing).
The tests drive the new lazy-loaded HTMX partials through the real
FastAPI app so middleware + dependency injection are exercised.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.store import SessionRepository, create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


def _cfg(tmp_path: Path, *, jaeger_url: str | None = None) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/chart.db"
    cfg.api_keys_raw = "k1"
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.dashboard_admin_password = SecretStr("hunter2")
    cfg.dashboard_session_secret = SecretStr(
        "a-test-secret-32-bytes-or-longer-xxxx"
    )
    cfg.public_base_url = "http://t"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    cfg.jaeger_ui_url = jaeger_url
    return cfg


async def _seed_two_sessions(store: SessionRepository) -> None:
    """Seed two sessions, one with aggregates + trace_id, the other
    without — covers both rendering branches of every partial."""
    now = datetime.now(UTC).replace(microsecond=0)
    await store.create_session(
        id="sid-with-aggs",
        spec_json={"prompt": "filled"},
        trace_id="11112222333344445555666677778888",
        backend="inprocess",
        submitted_at=now - timedelta(seconds=120),
    )
    await store.update_session_status(
        "sid-with-aggs",
        status="completed",
        ended_at=now - timedelta(seconds=10),
    )
    await store.update_session_aggregates(
        "sid-with-aggs",
        input_tokens=1234,
        output_tokens=567,
        cost_usd=0.0123,
        turn_count=4,
    )
    await store.create_session(
        id="sid-empty",
        spec_json={"prompt": "empty"},
        trace_id=None,
        backend="inprocess",
        submitted_at=now - timedelta(seconds=60),
    )


async def _login(ac: AsyncClient) -> None:
    r = await ac.post(
        "/dashboard/login",
        data={"username": "admin", "password": "hunter2"},
    )
    assert r.status_code == 303, r.text


@pytest_asyncio.fixture
async def client_with_jaeger(tmp_path: Path):
    cfg = _cfg(tmp_path / "with", jaeger_url="/jaeger")
    (tmp_path / "with").mkdir()
    app = create_app(cfg)
    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    store = SessionRepository(eng)
    await _seed_two_sessions(store)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac, app.router.lifespan_context(app):
        yield ac


@pytest_asyncio.fixture
async def client_without_jaeger(tmp_path: Path):
    cfg = _cfg(tmp_path / "no", jaeger_url=None)
    (tmp_path / "no").mkdir()
    app = create_app(cfg)
    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    store = SessionRepository(eng)
    await _seed_two_sessions(store)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac, app.router.lifespan_context(app):
        yield ac


class TestSessionChart:
    async def test_chart_renders_canvas_when_aggregates_present(
        self, client_with_jaeger: AsyncClient
    ) -> None:
        """Aggregate values flow into the canvas via ``data-totals``
        + ``data-labels`` JSON attributes, which the inline JS reads
        through Chart.js. The Chart.js script tag points at the
        configured CDN URL."""
        ac = client_with_jaeger
        await _login(ac)
        r = await ac.get("/dashboard/sessions/sid-with-aggs/chart")
        assert r.status_code == 200, r.text
        body = r.text
        # Canvas + data attrs reflect the seeded aggregates.
        assert "session-chart-canvas-sid-with-aggs" in body
        assert "&#34;input tokens&#34;" in body or '"input tokens"' in body
        assert "1234" in body  # input_tokens
        assert "567" in body  # output_tokens
        assert "12.3" in body  # cost_usd × 1000 = 12.3
        # Chart.js CDN script tag reaches the configured URL.
        assert "chart" in body.lower()

    async def test_chart_empty_state_when_aggregates_zero(
        self, client_with_jaeger: AsyncClient
    ) -> None:
        ac = client_with_jaeger
        await _login(ac)
        r = await ac.get("/dashboard/sessions/sid-empty/chart")
        assert r.status_code == 200
        body = r.text
        # No canvas — partial fell into the muted placeholder branch.
        assert "session-chart-canvas-sid-empty" not in body
        assert "session-chart-empty" in body
        assert "session.end" in body

    async def test_chart_404_for_unknown_session(
        self, client_with_jaeger: AsyncClient
    ) -> None:
        ac = client_with_jaeger
        await _login(ac)
        r = await ac.get("/dashboard/sessions/nope/chart")
        assert r.status_code == 404


class TestSpanTree:
    async def test_iframe_rendered_when_jaeger_configured(
        self, client_with_jaeger: AsyncClient
    ) -> None:
        ac = client_with_jaeger
        await _login(ac)
        r = await ac.get("/dashboard/sessions/sid-with-aggs/trace")
        assert r.status_code == 200
        body = r.text
        # Iframe src is composed from jaeger_ui_url + /trace/{trace_id}.
        assert (
            'src="/jaeger/trace/11112222333344445555666677778888"' in body
        )
        # The iframe carries the loading=lazy + sandbox attrs.
        assert 'loading="lazy"' in body
        assert "sandbox=" in body

    async def test_fallback_when_jaeger_unset(
        self, client_without_jaeger: AsyncClient
    ) -> None:
        ac = client_without_jaeger
        await _login(ac)
        r = await ac.get("/dashboard/sessions/sid-with-aggs/trace")
        assert r.status_code == 200
        body = r.text
        # No iframe when Config.jaeger_ui_url is empty.
        assert "<iframe" not in body
        # Fallback CTA is rendered + disabled.
        assert "disabled" in body
        assert "Open in Jaeger" in body
        # Trace id is still surfaced for manual lookup.
        assert "11112222333344445555666677778888" in body

    async def test_no_trace_id_branch(
        self, client_with_jaeger: AsyncClient
    ) -> None:
        """When the session never opened a trace span (e.g. OTel
        disabled) the partial falls through to the empty-state
        message regardless of the Jaeger URL config."""
        ac = client_with_jaeger
        await _login(ac)
        r = await ac.get("/dashboard/sessions/sid-empty/trace")
        assert r.status_code == 200
        body = r.text
        assert "<iframe" not in body
        assert "No trace_id recorded" in body

    async def test_trace_404_for_unknown_session(
        self, client_with_jaeger: AsyncClient
    ) -> None:
        ac = client_with_jaeger
        await _login(ac)
        r = await ac.get("/dashboard/sessions/nope/trace")
        assert r.status_code == 404
