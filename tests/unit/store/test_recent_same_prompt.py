"""Unit tests for :meth:`SqlAlchemyStore.recent_same_prompt` (Plan 8 D8.14 / Task 16).

Three black-box tests over a fresh on-disk SQLite engine — same fixture
shape as :mod:`tests.unit.store.test_search_query` — covering the
duplicate-prompt warning's three observable rules:

* a same-owner same-prompt-prefix row submitted within the last
  ``within_minutes`` is returned;
* a same-owner same-prompt-prefix row submitted *outside* the window
  is excluded;
* a same-prompt row owned by a different user is excluded (no
  cross-owner leakage).
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
    eng = make_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/recent-prompt.db"
    )
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
    owner: str,
    submitted_at: datetime,
) -> None:
    await store.create_session(
        id=sid,
        spec_json={"prompt": prompt},
        trace_id=None,
        backend="inprocess",
        owner=owner,
        submitted_at=submitted_at,
    )


async def test_recent_same_prompt_matches_within_window(
    store: SqlAlchemyStore,
) -> None:
    """A row submitted ~5 min ago with the same prompt prefix is
    returned — the default ``within_minutes=10`` covers it."""
    now = datetime.now(UTC)
    await _seed(
        store,
        sid="s1",
        prompt="deploy prod canary first",
        owner="alice",
        submitted_at=now - timedelta(minutes=5),
    )

    matches = await store.recent_same_prompt(
        owner="alice", prompt="deploy prod canary first"
    )
    assert len(matches) == 1
    assert matches[0]["id"] == "s1"
    assert matches[0]["status"] == "queued"
    assert matches[0]["submitted_at"] is not None


async def test_recent_same_prompt_excludes_past_window(
    store: SqlAlchemyStore,
) -> None:
    """A row submitted 30 min ago is outside the default 10-min window
    and is NOT returned — same prompt + same owner."""
    now = datetime.now(UTC)
    await _seed(
        store,
        sid="s_old",
        prompt="this is yesterday's request body",
        owner="alice",
        submitted_at=now - timedelta(minutes=30),
    )

    matches = await store.recent_same_prompt(
        owner="alice", prompt="this is yesterday's request body"
    )
    assert matches == []


async def test_recent_same_prompt_excludes_other_owner(
    store: SqlAlchemyStore,
) -> None:
    """A same-prompt row owned by a *different* user must NOT leak to
    the current owner's duplicate warning."""
    now = datetime.now(UTC)
    await _seed(
        store,
        sid="bobs",
        prompt="kick the cache",
        owner="bob",
        submitted_at=now - timedelta(minutes=2),
    )

    matches = await store.recent_same_prompt(
        owner="alice", prompt="kick the cache"
    )
    assert matches == []
