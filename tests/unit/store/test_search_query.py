"""Unit tests for :meth:`SqlAlchemyStore.search_sessions` (Plan 8 D8.20 / Task 12).

Three black-box tests over a fresh on-disk SQLite engine (same shape as
``test_cursor_pagination.py``) covering the three filter axes the
search endpoint promises:

* ``q`` is case-insensitive and matches against the JSON spec payload
  (where the prompt lives).
* ``owner`` and ``status`` filters compose with AND semantics.
* The cursor delivers consistent pagination across ~60 seeded rows.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.store import (
    SqlAlchemyStore,
    create_all_tables,
    make_async_engine,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncEngine:
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/search.db")
    await create_all_tables(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def store(engine: AsyncEngine) -> SqlAlchemyStore:
    return SqlAlchemyStore(engine)


async def _seed(
    store: SqlAlchemyStore,
    *,
    sid: str,
    prompt: str,
    owner: str = "alice",
    tags: tuple[str, ...] = (),
    status: str | None = None,
    submitted_at: datetime | None = None,
) -> None:
    """Seed one session row with an explicit spec_json.prompt."""
    await store.create_session(
        id=sid,
        spec_json={"prompt": prompt},
        trace_id=None,
        backend="inprocess",
        tags=tags,
        owner=owner,
        submitted_at=submitted_at,
    )
    if status is not None:
        await store.update_session_status(sid, status=status)


async def test_search_by_prompt_case_insensitive(
    store: SqlAlchemyStore,
) -> None:
    """``q='WORLD'`` (upper-case) must match a row whose stored prompt is
    ``'hello world'`` — the search is case-insensitive via
    ``func.lower(cast(spec_json, String))``."""
    await _seed(store, sid="s1", prompt="hello world")
    await _seed(store, sid="s2", prompt="goodbye sky")
    await _seed(store, sid="s3", prompt="HELLO WORLD again")

    rows, nxt = await store.search_sessions(q="WORLD")
    ids = {r["id"] for r in rows}
    assert ids == {"s1", "s3"}
    assert nxt is None


async def test_search_filter_combined_owner_status(
    store: SqlAlchemyStore,
) -> None:
    """Combined filters AND together: ``owner='alice' + status=['running']``
    excludes alice/failed and bob/running."""
    await _seed(
        store, sid="r1", prompt="x", owner="alice", status="running"
    )
    await _seed(store, sid="r2", prompt="y", owner="alice", status="failed")
    await _seed(
        store, sid="r3", prompt="z", owner="bob", status="running"
    )

    rows, _ = await store.search_sessions(
        owner="alice", status=["running"]
    )
    assert [r["id"] for r in rows] == ["r1"]


async def test_search_cursor_pagination(store: SqlAlchemyStore) -> None:
    """60 seeded rows page out as 50 + 10 with a stable cursor; no rows
    are dropped or duplicated and the second page exhausts the set."""
    base = datetime.now(UTC).replace(microsecond=0)
    for i in range(60):
        await _seed(
            store,
            sid=f"p{i:03d}",
            prompt=f"prompt {i}",
            submitted_at=base - timedelta(seconds=i),
        )

    page1, c1 = await store.search_sessions(limit=50)
    assert len(page1) == 50
    assert c1 is not None

    page2, c2 = await store.search_sessions(limit=50, after=c1)
    assert len(page2) == 10
    assert c2 is None

    ids_p1 = {r["id"] for r in page1}
    ids_p2 = {r["id"] for r in page2}
    assert ids_p1.isdisjoint(ids_p2)
    assert len(ids_p1 | ids_p2) == 60
