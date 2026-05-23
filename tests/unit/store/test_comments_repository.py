"""Unit tests for SqlAlchemyStore comment methods — Plan 8 D8.5 / Task 7.

Exercises the new :meth:`SqlAlchemyStore.create_comment` /
:meth:`list_comments` / :meth:`get_comment` / :meth:`update_comment` /
:meth:`soft_delete_comment` surface. Fixture pattern mirrors
:mod:`tests.unit.store.test_audit_repository` — a fresh on-disk
SQLite per test under ``tmp_path`` so multiple connections behave
like production.

Tests:

  * ``test_create_returns_full_row`` — write returns id + timestamps
    + initial ``deleted_at=None``.
  * ``test_list_orders_oldest_first`` — three comments seeded with
    spaced ``created_at`` round-trip in ascending order.
  * ``test_list_excludes_soft_deleted`` — soft-deleted rows are
    hidden by default, surfaced via ``include_deleted=True``.
  * ``test_get_returns_row_including_tombstone`` — ``get_comment``
    returns soft-deleted rows so the moderation path can read them.
  * ``test_update_bumps_updated_at_and_blocks_deleted`` — successful
    update bumps ``updated_at``; a follow-up update on a soft-deleted
    row returns False.
  * ``test_soft_delete_idempotent`` — second soft-delete returns
    False without raising.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.store import (
    SqlAlchemyStore,
    create_all_tables,
    make_async_engine,
    session_comments,
    sessions,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncEngine:
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/comments.db")
    await create_all_tables(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def store(engine: AsyncEngine) -> SqlAlchemyStore:
    return SqlAlchemyStore(engine)


async def _seed_session(engine: AsyncEngine, sid: str = "sid-comments") -> str:
    """Insert a parent session row so the FK on ``session_comments``
    is satisfied. Returns the session id."""
    async with engine.begin() as conn:
        await conn.execute(
            sessions.insert().values(
                id=sid,
                status="queued",
                spec_json={},
                tags=[],
                submitted_at=datetime.now(UTC),
                backend="inprocess",
            )
        )
    return sid


class TestCreateComment:
    async def test_create_returns_full_row(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        sid = await _seed_session(engine)
        row = await store.create_comment(
            session_id=sid,
            author="alice",
            body_markdown="**hello**",
            body_html="<p><strong>hello</strong></p>",
        )
        assert isinstance(row["id"], int) and row["id"] > 0
        assert row["session_id"] == sid
        assert row["author"] == "alice"
        assert row["body_markdown"] == "**hello**"
        assert row["body_html"] == "<p><strong>hello</strong></p>"
        assert row["deleted_at"] is None
        assert row["created_at"] is not None
        assert row["updated_at"] is not None

        # Round-trip through the table to make sure the row landed.
        async with engine.connect() as conn:
            db = (
                await conn.execute(
                    session_comments.select().where(
                        session_comments.c.id == row["id"]
                    )
                )
            ).mappings().first()
        assert db is not None
        assert db["body_markdown"] == "**hello**"


class TestListComments:
    async def test_list_orders_oldest_first(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        sid = await _seed_session(engine)
        # Three sequential creates with a small sleep so the
        # ``created_at`` timestamps differ — SQLite's datetime
        # precision is microsecond on the bundled driver but the
        # sleeps keep the test robust across slow runners.
        for body in ("first", "second", "third"):
            await store.create_comment(
                session_id=sid,
                author="alice",
                body_markdown=body,
                body_html=f"<p>{body}</p>",
            )
            await asyncio.sleep(0.01)
        rows = await store.list_comments(session_id=sid)
        assert [r["body_markdown"] for r in rows] == [
            "first",
            "second",
            "third",
        ]

    async def test_list_excludes_soft_deleted_by_default(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        sid = await _seed_session(engine)
        keep = await store.create_comment(
            session_id=sid,
            author="alice",
            body_markdown="keep",
            body_html="<p>keep</p>",
        )
        delete = await store.create_comment(
            session_id=sid,
            author="alice",
            body_markdown="delete",
            body_html="<p>delete</p>",
        )
        ok = await store.soft_delete_comment(comment_id=delete["id"])
        assert ok is True

        live = await store.list_comments(session_id=sid)
        assert [r["id"] for r in live] == [keep["id"]]

        all_rows = await store.list_comments(
            session_id=sid, include_deleted=True
        )
        assert {r["id"] for r in all_rows} == {keep["id"], delete["id"]}

    async def test_list_other_session_returns_empty(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        sid_a = await _seed_session(engine, "sid-a")
        sid_b = await _seed_session(engine, "sid-b")
        await store.create_comment(
            session_id=sid_a,
            author="alice",
            body_markdown="A",
            body_html="<p>A</p>",
        )
        rows_b = await store.list_comments(session_id=sid_b)
        assert rows_b == []


class TestGetComment:
    async def test_get_returns_row_including_tombstone(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        sid = await _seed_session(engine)
        created = await store.create_comment(
            session_id=sid,
            author="alice",
            body_markdown="moderate-me",
            body_html="<p>moderate-me</p>",
        )
        await store.soft_delete_comment(comment_id=created["id"])
        # ``get_comment`` must STILL return the row so the
        # moderation path can read its content / author.
        got = await store.get_comment(comment_id=created["id"])
        assert got is not None
        assert got["id"] == created["id"]
        assert got["deleted_at"] is not None
        assert got["body_markdown"] == "moderate-me"


class TestUpdateComment:
    async def test_update_bumps_updated_at(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        sid = await _seed_session(engine)
        created = await store.create_comment(
            session_id=sid,
            author="alice",
            body_markdown="original",
            body_html="<p>original</p>",
        )
        original_updated_at = created["updated_at"]
        await asyncio.sleep(0.01)
        ok = await store.update_comment(
            comment_id=created["id"],
            body_markdown="edited",
            body_html="<p>edited</p>",
        )
        assert ok is True
        got = await store.get_comment(comment_id=created["id"])
        assert got is not None
        assert got["body_markdown"] == "edited"
        assert got["body_html"] == "<p>edited</p>"
        # The ``updated_at`` clock must advance strictly. SQLite
        # rehydrates the timestamp without tzinfo whereas the
        # freshly-inserted ``original`` is tz-aware, so we strip the
        # tz before comparing — both are UTC by construction.
        got_naive = got["updated_at"].replace(tzinfo=None)
        original_naive = original_updated_at.replace(tzinfo=None)
        assert got_naive > original_naive

    async def test_update_blocks_soft_deleted(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        sid = await _seed_session(engine)
        created = await store.create_comment(
            session_id=sid,
            author="alice",
            body_markdown="original",
            body_html="<p>original</p>",
        )
        await store.soft_delete_comment(comment_id=created["id"])
        # A tombstoned row must NOT be editable — the router maps
        # the False return to 409 / 404.
        ok = await store.update_comment(
            comment_id=created["id"],
            body_markdown="zombie edit",
            body_html="<p>zombie edit</p>",
        )
        assert ok is False
        got = await store.get_comment(comment_id=created["id"])
        assert got is not None
        # Body must NOT have changed.
        assert got["body_markdown"] == "original"


class TestSoftDelete:
    async def test_soft_delete_idempotent(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        sid = await _seed_session(engine)
        created = await store.create_comment(
            session_id=sid,
            author="alice",
            body_markdown="x",
            body_html="<p>x</p>",
        )
        first = await store.soft_delete_comment(comment_id=created["id"])
        second = await store.soft_delete_comment(comment_id=created["id"])
        assert first is True
        assert second is False, (
            "second soft-delete should return False (no live row matched)"
        )
