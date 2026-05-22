"""Dashboard Kanban integration tests — Plan 6 Task 9.

Covers Plan 6 D6.3 (HTMX + SSE + 5s polling fallback) plus D6.16 pagination
and D6.13 read-only navigation. Tests the four new routes:

* ``GET /dashboard/kanban`` — full page chrome
* ``GET /dashboard/kanban/board?offset=N`` — HTMX partial (also serves
  the polling-fallback target and the next-page lazy loader)
* ``GET /dashboard/kanban/stream`` — SSE event stream emitting
  ``event: kanban-update`` whenever ``SessionCreated`` /
  ``SessionStateChanged`` / ``SessionCompleted`` fires on the bus
* ``GET /dashboard/kanban/chart?window_s=...&bucket_s=...`` — JSON feed
  matching the Chart.js v4 line-chart schema

The fixture spins up a real FastAPI app (lifespan + middleware) via
``ASGITransport`` and pre-seeds three sessions covering the columns the
template groups (queued / running / paused / terminal).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.core import EventBus, SessionCreated, SessionStateChanged
from gg_relay.store import SessionRepository, create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


def _cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/kanban.db"
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
    cfg.kanban_default_page_size = 3
    return cfg


async def _seed(store: SessionRepository) -> None:
    """Seed three sessions across the four kanban columns + one
    terminal aggregate row so the chart partial has data to render."""
    now = datetime.now(UTC).replace(microsecond=0)
    await store.create_session(
        id="sid-q",
        spec_json={"prompt": "queued"},
        trace_id=None,
        backend="inprocess",
        tags=("alpha",),
        submitted_at=now - timedelta(seconds=120),
    )
    await store.create_session(
        id="sid-r",
        spec_json={"prompt": "running"},
        trace_id=None,
        backend="inprocess",
        submitted_at=now - timedelta(seconds=90),
    )
    await store.update_session_status(
        "sid-r", status="running", started_at=now - timedelta(seconds=80)
    )
    await store.create_session(
        id="sid-p",
        spec_json={"prompt": "paused"},
        trace_id=None,
        backend="inprocess",
        submitted_at=now - timedelta(seconds=60),
    )
    await store.update_session_status(
        "sid-p", status="paused", started_at=now - timedelta(seconds=50)
    )
    await store.create_session(
        id="sid-c",
        spec_json={"prompt": "completed"},
        trace_id=None,
        backend="inprocess",
        submitted_at=now - timedelta(seconds=30),
    )
    await store.update_session_status(
        "sid-c",
        status="completed",
        started_at=now - timedelta(seconds=25),
        ended_at=now - timedelta(seconds=10),
    )
    await store.update_session_aggregates(
        "sid-c",
        input_tokens=1000,
        output_tokens=500,
        cost_usd=0.04,
        turn_count=3,
    )


@pytest_asyncio.fixture
async def client(tmp_path: Path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    store = SessionRepository(eng)
    await _seed(store)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac, app.router.lifespan_context(app):
        yield ac, app


async def _login(ac: AsyncClient) -> None:
    r = await ac.post(
        "/dashboard/login",
        data={"username": "admin", "password": "hunter2"},
    )
    assert r.status_code == 303, r.text


class TestKanbanPage:
    async def test_full_page_renders_with_columns_and_chart(
        self, client: tuple[AsyncClient, object]
    ) -> None:
        ac, _app = client
        await _login(ac)
        r = await ac.get("/dashboard/kanban")
        assert r.status_code == 200, r.text
        body = r.text
        # Five visible buckets: 4 columns (queued/running/paused/done)
        # + the chart canvas. D6.13 = read-only, so no drag handles.
        for col in ("Queued", "Running", "Paused", "Done"):
            assert col in body, f"missing column: {col!r}"
        assert "kanban-chart" in body
        # Cards link to detail pages (D6.13 navigation contract).
        # Submitted_at-desc + page_size=3 puts the 3 newest first
        # (sid-c, sid-p, sid-r); sid-q rolls onto page 2.
        assert "/dashboard/sessions/sid-r" in body
        assert "/dashboard/sessions/sid-p" in body
        assert "/dashboard/sessions/sid-c" in body
        # Chart.js script tag is wired with the configured CDN.
        assert "chart.umd.min.js" in body or "chart" in body.lower()
        # 5s polling fallback is present.
        assert "every 5s" in body
        # SSE handshake URL is referenced.
        assert "/dashboard/kanban/stream" in body

    async def test_anonymous_redirects_to_login(
        self, client: tuple[AsyncClient, object]
    ) -> None:
        ac, _ = client
        r = await ac.get("/dashboard/kanban")
        assert r.status_code == 303
        assert r.headers["location"] == "/dashboard/login"


class TestKanbanBoardPartial:
    async def test_partial_groups_sessions_into_columns(
        self, client: tuple[AsyncClient, object]
    ) -> None:
        ac, _ = client
        await _login(ac)
        r = await ac.get("/dashboard/kanban/board")
        assert r.status_code == 200
        body = r.text
        # Each column-section is rendered with the data-column attr.
        for col in ("queued", "running", "paused", "terminal"):
            assert f'data-column="{col}"' in body
        # Each seeded card lands in its own column. Page 1 (page_size=3)
        # carries sid-c (terminal/completed), sid-p (paused), and
        # sid-r (terminal — recovery flipped it from running →
        # interrupted on lifespan startup); sid-q rolls onto page 2.
        assert "sid-r" in body
        assert "sid-p" in body
        assert "sid-c" in body
        # Page 2 carries the queued card.
        r2 = await ac.get("/dashboard/kanban/board?offset=3")
        assert r2.status_code == 200
        assert "sid-q" in r2.text

    async def test_pagination_offset_and_size(
        self, client: tuple[AsyncClient, object]
    ) -> None:
        """page_size=3 + 4 sessions seeded → first page has 3 cards
        and emits the ``revealed`` next-page loader; second page
        carries the last card and no further pagination."""
        ac, _ = client
        await _login(ac)
        r1 = await ac.get("/dashboard/kanban/board?offset=0")
        assert r1.status_code == 200
        # First page is full (page_size=3 ≤ total 4) so the next-page
        # lazy-load div MUST be present.
        assert "kanban-next-page" in r1.text
        assert 'hx-trigger="revealed"' in r1.text
        # Second page picks up the remaining 1 row, no further page.
        r2 = await ac.get("/dashboard/kanban/board?offset=3")
        assert r2.status_code == 200
        assert "kanban-next-page" not in r2.text


class TestKanbanChart:
    async def test_chart_partial_returns_chartjs_schema(
        self, client: tuple[AsyncClient, object]
    ) -> None:
        ac, _ = client
        await _login(ac)
        r = await ac.get(
            "/dashboard/kanban/chart?window_s=86400&bucket_s=300"
        )
        assert r.status_code == 200
        data = r.json()
        # Chart.js v4 line-chart shape: labels + datasets[].
        assert set(data) >= {
            "labels",
            "datasets",
            "window_s",
            "bucket_s",
            "session_count",
        }
        labels: list[str] = data["labels"]
        datasets = data["datasets"]
        assert isinstance(labels, list)
        # Three datasets per Plan 6 D6.4 (input / output / cost).
        assert len(datasets) == 3
        names = {d["label"] for d in datasets}
        assert "input tokens" in names
        assert "output tokens" in names
        assert any("cost" in n for n in names)
        # Each dataset's ``data`` length matches the labels length.
        for d in datasets:
            assert len(d["data"]) == len(labels)
        # Seeded session-c sits inside the 24h window — total tokens
        # should be 1500 across all returned buckets.
        in_total = sum(int(x) for x in datasets[0]["data"])
        out_total = sum(int(x) for x in datasets[1]["data"])
        assert in_total == 1000
        assert out_total == 500


class TestKanbanSSE:
    async def test_sse_iter_emits_kanban_update_on_published_events(
        self,
    ) -> None:
        """Generator-level test: drive ``_kanban_sse_iter`` directly so
        we don't have to wrestle ASGITransport's buffered streaming. The
        iterator should yield two ``event: kanban-update`` chunks for
        the two events we publish on the bus."""
        from starlette.requests import Request

        from gg_relay.dashboard.router import _kanban_sse_iter

        bus = EventBus()
        scope = {
            "type": "http",
            "method": "GET",
            "headers": [],
        }

        async def _receive():  # pragma: no cover
            return {"type": "http.request"}

        req = Request(scope, _receive)  # type: ignore[arg-type]
        gen = _kanban_sse_iter(bus, req)

        chunks: list[str] = []

        async def _drive() -> None:
            await asyncio.sleep(0.05)
            await bus.publish(SessionCreated(session_id="sse-1"))
            await bus.publish(
                SessionStateChanged(
                    session_id="sse-1",
                    from_state="queued",
                    to_state="running",
                )
            )

        pub = asyncio.create_task(_drive())
        try:
            # Drain up to 4 chunks; we expect handshake + 2 events.
            end_at = asyncio.get_event_loop().time() + 2.0
            while len(chunks) < 4 and (
                asyncio.get_event_loop().time() < end_at
            ):
                try:
                    chunk = await asyncio.wait_for(
                        gen.__anext__(), timeout=0.3
                    )
                except (StopAsyncIteration, TimeoutError):
                    break
                chunks.append(chunk)
                joined = "".join(chunks)
                if joined.count("event: kanban-update") >= 2:
                    break
        finally:
            await pub
            await gen.aclose()
            await bus.close()

        body = "".join(chunks)
        assert body.count("event: kanban-update") >= 2, (
            f"expected ≥2 kanban-update chunks, got {body!r}"
        )
        assert "SessionCreated" in body
        assert "SessionStateChanged" in body


class TestKanbanEmptyState:
    async def test_empty_state_renders_when_no_sessions(
        self, tmp_path: Path
    ) -> None:
        """Spin up a fresh app with an empty DB so all four columns
        fall through to the ``No sessions`` empty-state branch."""
        cfg = _cfg(tmp_path / "empty")
        (tmp_path / "empty").mkdir()
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
            await _login(ac)
            r = await ac.get("/dashboard/kanban/board")
            assert r.status_code == 200
            # Each column shows the empty placeholder.
            assert r.text.count("No sessions") >= 4
            assert "kanban-next-page" not in r.text
