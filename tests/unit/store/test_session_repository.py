"""Unit tests for the Plan 8 D8.6 / Task 9 ``parent_session_id`` surface.

Exercises :meth:`SqlAlchemyStore.create_session` (now accepting
``parent_session_id``) and :meth:`SqlAlchemyStore.list_children_of_session`,
the new helper that walks one level down a retry tree.

Fixture pattern mirrors :mod:`tests.unit.store.test_comments_repository`
— a fresh on-disk SQLite per test under ``tmp_path`` so multiple
connections behave like production. Lives in its own file to keep
the existing :mod:`tests.unit.store.test_store` module focused on
the pre-Task-9 contract.
"""
from __future__ import annotations

import asyncio
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
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/parent.db")
    await create_all_tables(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def store(engine: AsyncEngine) -> SqlAlchemyStore:
    return SqlAlchemyStore(engine)


class TestCreateSessionWithParent:
    async def test_create_session_with_parent_session_id(
        self, store: SqlAlchemyStore
    ) -> None:
        """Inserting a child row stamps ``parent_session_id`` and
        ``get_session`` round-trips it back unchanged."""
        await store.create_session(
            id="parent-sid",
            spec_json={"prompt": "hi"},
            trace_id=None,
            backend="inprocess",
        )
        await store.create_session(
            id="child-sid",
            spec_json={"prompt": "hi"},
            trace_id=None,
            backend="inprocess",
            parent_session_id="parent-sid",
        )
        parent_row = await store.get_session("parent-sid")
        child_row = await store.get_session("child-sid")
        assert parent_row is not None
        assert child_row is not None
        # Parent has no lineage of its own.
        assert parent_row["parent_session_id"] is None
        # Child links back to parent verbatim.
        assert child_row["parent_session_id"] == "parent-sid"


class TestListChildrenOfSession:
    async def test_list_children_of_session(
        self, store: SqlAlchemyStore
    ) -> None:
        """Two children of the same parent + an unrelated session
        round-trip through :meth:`list_children_of_session` returning
        both children in submission order, excluding the unrelated
        row."""
        await store.create_session(
            id="root",
            spec_json={"prompt": "root"},
            trace_id=None,
            backend="inprocess",
        )
        await store.create_session(
            id="child-1",
            spec_json={"prompt": "c1"},
            trace_id=None,
            backend="inprocess",
            parent_session_id="root",
        )
        # Sleep a hair so the two retries get distinct submitted_at
        # timestamps and we can assert on the chronological order.
        await asyncio.sleep(0.01)
        await store.create_session(
            id="child-2",
            spec_json={"prompt": "c2"},
            trace_id=None,
            backend="inprocess",
            parent_session_id="root",
        )
        await store.create_session(
            id="unrelated",
            spec_json={"prompt": "x"},
            trace_id=None,
            backend="inprocess",
            parent_session_id="some-other-sid",
        )
        children = await store.list_children_of_session(
            parent_session_id="root"
        )
        assert [r["id"] for r in children] == ["child-1", "child-2"], (
            "children must come back in submission order"
        )
        # Sanity — the unrelated row stayed out of the result set.
        ids = {r["id"] for r in children}
        assert "unrelated" not in ids

    async def test_list_children_returns_empty_for_unknown_parent(
        self, store: SqlAlchemyStore
    ) -> None:
        """An unknown / archived parent yields an empty list (not None)."""
        await store.create_session(
            id="lonely",
            spec_json={"prompt": "x"},
            trace_id=None,
            backend="inprocess",
        )
        result = await store.list_children_of_session(
            parent_session_id="not-a-real-sid"
        )
        assert result == []
