"""Cursor pagination unit tests — Plan 7 Task 9 (D7.6).

Exercises :meth:`SqlAlchemyStore.list_sessions` paging contract:

* first / next / exhausted pages return the expected rows + cursor
* malformed cursors raise :class:`CursorInvalidError` (router → 400)
* cursors minted under a different filter raise
  :class:`CursorFilterMismatchError` (router → 400)
* ``id`` is the stable tiebreaker when two rows share ``submitted_at``
* SQL-side tag filtering returns the right rows even with cursor paging
* the dashboard kanban partial emits a cursor-shaped lazy-load URL

Tests use a fresh on-disk SQLite per case (the same shape as
``test_store.py``) so paging behaves like production where multiple
connections may serve a single page.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.store import (
    CursorFilterMismatchError,
    CursorInvalidError,
    SqlAlchemyStore,
    create_all_tables,
    make_async_engine,
)


pytestmark = pytest.mark.asyncio


# ── fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncEngine:
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/cursor.db")
    await create_all_tables(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def store(engine: AsyncEngine) -> SqlAlchemyStore:
    return SqlAlchemyStore(engine)


async def _seed_n(
    store: SqlAlchemyStore,
    *,
    n: int,
    base: datetime,
    tags: tuple[str, ...] = (),
    status: str | None = None,
) -> list[str]:
    """Seed ``n`` queued sessions with strictly-decreasing submit times.

    Returns the ids in newest-first order so tests can assert pagination
    deterministically. Sessions are spaced one second apart starting at
    ``base`` and counting downward, so the newest carries the smallest
    suffix and the oldest the largest.
    """
    ids: list[str] = []
    for i in range(n):
        sid = f"s{i:03d}"
        await store.create_session(
            id=sid,
            spec_json={},
            trace_id=None,
            backend="inprocess",
            tags=tags,
            submitted_at=base - timedelta(seconds=i),
        )
        if status is not None:
            await store.update_session_status(sid, status=status)
        ids.append(sid)
    return ids


# ── pagination happy path ─────────────────────────────────────────────


class TestCursorPagination:
    async def test_first_page(self, store: SqlAlchemyStore) -> None:
        """Empty cursor returns the first ``limit`` rows + a next cursor
        whenever the result set has more than ``limit`` rows."""
        ids = await _seed_n(store, n=120, base=datetime.now(UTC))
        rows, nxt = await store.list_sessions(limit=50)
        assert [r["id"] for r in rows] == ids[:50]
        assert nxt is not None and isinstance(nxt, str) and len(nxt) > 0

    async def test_next_page(self, store: SqlAlchemyStore) -> None:
        """Passing the previous ``next_cursor`` back as ``after``
        yields the next contiguous slice with no skips or repeats."""
        ids = await _seed_n(store, n=120, base=datetime.now(UTC))
        page1, c1 = await store.list_sessions(limit=50)
        assert c1 is not None
        page2, _ = await store.list_sessions(limit=50, after=c1)
        # Same row never appears on two pages.
        page1_ids = [r["id"] for r in page1]
        page2_ids = [r["id"] for r in page2]
        assert not (set(page1_ids) & set(page2_ids))
        assert page1_ids + page2_ids == ids[:100]

    async def test_exhausted(self, store: SqlAlchemyStore) -> None:
        """Last page returns ``next_cursor=None`` so clients know to
        stop without an extra round-trip."""
        ids = await _seed_n(store, n=7, base=datetime.now(UTC))
        # limit > total → first call exhausts the set.
        rows, nxt = await store.list_sessions(limit=10)
        assert [r["id"] for r in rows] == ids
        assert nxt is None

    # ── error mapping ────────────────────────────────────────────

    async def test_invalid_cursor(self, store: SqlAlchemyStore) -> None:
        """Garbage in the ``after`` slot raises CursorInvalidError —
        the API router maps this to HTTP 400 ``cursor_invalid``."""
        with pytest.raises(CursorInvalidError):
            await store.list_sessions(after="!!! not base64 !!!")

    async def test_cursor_filter_mismatch(
        self, store: SqlAlchemyStore
    ) -> None:
        """A cursor minted under one ``status`` cannot be reused under
        another — guards against operators paging across a filter
        change and getting confusing mixed results."""
        base = datetime.now(UTC)
        # 12 running rows so the first running-filter page has a cursor.
        await _seed_n(store, n=12, base=base, status="running")
        _, c_running = await store.list_sessions(
            status="running", limit=10
        )
        assert c_running is not None
        # Re-using c_running under status=failed must fail.
        with pytest.raises(CursorFilterMismatchError):
            await store.list_sessions(
                status="failed", limit=10, after=c_running
            )

    # ── stability ────────────────────────────────────────────────

    async def test_stability_with_same_submitted_at(
        self, store: SqlAlchemyStore
    ) -> None:
        """Two rows sharing ``submitted_at`` are still paged
        deterministically — ``id`` is the secondary sort key so the
        cursor unambiguously identifies the last row regardless of
        clock granularity."""
        same_ts = datetime.now(UTC).replace(microsecond=0)
        # 6 rows, same submitted_at, distinct ids — list newest-first.
        ids = ["zzz", "ddd", "ccc", "bbb", "aaa", "000"]
        for sid in ids:
            await store.create_session(
                id=sid,
                spec_json={},
                trace_id=None,
                backend="inprocess",
                submitted_at=same_ts,
            )
        page1, c1 = await store.list_sessions(limit=3)
        assert c1 is not None
        # ORDER BY submitted_at DESC, id DESC → zzz, ddd, ccc.
        assert [r["id"] for r in page1] == ["zzz", "ddd", "ccc"]
        page2, c2 = await store.list_sessions(limit=3, after=c1)
        # Next page: bbb, aaa, 000 — all 6 rows reached with no
        # duplicates and no missed rows.
        assert [r["id"] for r in page2] == ["bbb", "aaa", "000"]
        assert c2 is None

    # ── SQL-side tag filtering ───────────────────────────────────

    async def test_tag_sql_filter(self, store: SqlAlchemyStore) -> None:
        """``tag=foo`` filters SQL-side via JSON1 ``json_each`` so
        cursor pagination never silently drops or duplicates rows.

        Setup: 30 sessions tagged ``foo``, 30 tagged ``bar``. Page
        them out under ``tag=foo`` and confirm we get exactly the
        foo rows and no bar rows leak across pages.
        """
        base = datetime.now(UTC).replace(microsecond=0)
        foo_ids: list[str] = []
        bar_ids: list[str] = []
        # Interleave foo and bar at distinct submitted_at so order is
        # deterministic. ``foo`` rows go at even offsets (newer), bar
        # at odd offsets so paging must hop over bar rows correctly.
        for i in range(60):
            sid = f"t{i:03d}"
            tag = "foo" if i % 2 == 0 else "bar"
            await store.create_session(
                id=sid,
                spec_json={},
                trace_id=None,
                backend="inprocess",
                tags=(tag,),
                submitted_at=base - timedelta(seconds=i),
            )
            (foo_ids if tag == "foo" else bar_ids).append(sid)
        collected: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            rows, cursor = await store.list_sessions(
                tag="foo", limit=8, after=cursor
            )
            collected.extend(r["id"] for r in rows)
            for r in rows:
                # No bar rows ever returned.
                assert "bar" not in (r["tags"] or [])
            if cursor is None:
                break
        # All 30 foo rows surface exactly once, in newest-first order.
        assert collected == foo_ids
        assert len(collected) == 30


# ── dashboard partial integration ─────────────────────────────────────


def _dashboard_cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/cursor_dash.db"
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


@pytest_asyncio.fixture
async def dashboard_client(tmp_path: Path):
    cfg = _dashboard_cfg(tmp_path)
    app = create_app(cfg)
    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    store = SqlAlchemyStore(eng)
    base = datetime.now(UTC).replace(microsecond=0)
    # Seed 4 sessions so page_size=3 forces a cursor on page 1.
    for i in range(4):
        await store.create_session(
            id=f"k{i}",
            spec_json={},
            trace_id=None,
            backend="inprocess",
            submitted_at=base - timedelta(seconds=i),
        )
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac, app.router.lifespan_context(app):
        # login so _RequireSessionDep lets us through
        r = await ac.post(
            "/dashboard/login",
            data={"username": "admin", "password": "hunter2"},
        )
        assert r.status_code == 303, r.text
        yield ac


_BOARD_AFTER_RE = re.compile(
    r'hx-get="/dashboard/kanban/board\?after=([A-Za-z0-9_-]+)"'
)


async def test_dashboard_kanban_pagination(
    dashboard_client: AsyncClient,
) -> None:
    """HTMX ``hx-get`` returns HTML with an ``?after=<cursor>`` lazy-
    load link, and following that link yields the remaining row + no
    further pagination link (Plan 7 D7.6 / Task 9)."""
    r1 = await dashboard_client.get("/dashboard/kanban/board")
    assert r1.status_code == 200
    # First page (page_size=3) must emit the cursor lazy-load div.
    assert "kanban-next-page" in r1.text
    assert "?after=" in r1.text
    # No legacy offset URLs leak.
    assert "?offset=" not in r1.text
    m = _BOARD_AFTER_RE.search(r1.text)
    assert m is not None
    cursor = m.group(1)
    r2 = await dashboard_client.get(
        f"/dashboard/kanban/board?after={cursor}"
    )
    assert r2.status_code == 200
    # The remaining row lands and no further lazy-load div is emitted.
    assert "k3" in r2.text
    assert "kanban-next-page" not in r2.text
