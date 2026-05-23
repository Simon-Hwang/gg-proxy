"""Unit tests for SqlAlchemyStore favorite methods — Plan 8 D8.21 / Task 13.

Exercises the new :meth:`SqlAlchemyStore.add_favorite` /
:meth:`remove_favorite` / :meth:`is_favorited` / :meth:`list_favorites`
surface. Fixture pattern mirrors
:mod:`tests.unit.store.test_comments_repository` — a fresh on-disk
SQLite per test under ``tmp_path`` so multiple connections behave
like production.

Tests:

  * ``test_add_favorite_idempotent`` — first star returns True,
    second star (same pair) returns False; only one row materialises.
  * ``test_remove_favorite_idempotent`` — first un-star returns True;
    a second un-star (or one against a never-starred pair) returns
    False without raising.
  * ``test_list_favorites_ordered_recent_first`` — three favorites
    spaced in time round-trip newest-first.
  * ``test_cascade_delete_with_session`` — deleting the parent session
    cascades to its favorite rows (FK ON DELETE CASCADE; SQLite
    PRAGMA foreign_keys=ON enabled by ``make_async_engine``).
  * ``test_is_favorited_reflects_state`` — sanity round-trip for the
    cheap-lookup helper used by the kanban renderer.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.store import (
    SqlAlchemyStore,
    create_all_tables,
    make_async_engine,
    session_favorites,
    sessions,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncEngine:
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/favorites.db")
    await create_all_tables(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def store(engine: AsyncEngine) -> SqlAlchemyStore:
    return SqlAlchemyStore(engine)


async def _seed_session(
    engine: AsyncEngine,
    sid: str = "sid-fav",
    *,
    owner: str | None = None,
    prompt: str = "favored prompt",
) -> str:
    """Insert a parent session row so the FK on ``session_favorites``
    is satisfied. Returns the session id."""
    async with engine.begin() as conn:
        await conn.execute(
            sessions.insert().values(
                id=sid,
                status="queued",
                spec_json={"prompt": prompt},
                tags=[],
                submitted_at=datetime.now(UTC),
                backend="inprocess",
                owner=owner,
            )
        )
    return sid


class TestAddFavorite:
    async def test_add_favorite_idempotent(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        """First star returns True; second star returns False; only
        one row lands in the table."""
        sid = await _seed_session(engine)

        first = await store.add_favorite(
            session_id=sid, user_label="alice"
        )
        assert first is True

        second = await store.add_favorite(
            session_id=sid, user_label="alice"
        )
        assert second is False, (
            "second add_favorite on same pair must return False"
        )

        # Only one row materialised — the unique constraint blocked
        # the second insert and the repository swallowed the
        # IntegrityError into a clean False contract.
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    session_favorites.select().where(
                        session_favorites.c.session_id == sid
                    )
                )
            ).all()
        assert len(rows) == 1


class TestRemoveFavorite:
    async def test_remove_favorite_idempotent(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        """First remove returns True; second remove (or one against a
        never-starred pair) returns False without raising."""
        sid = await _seed_session(engine)
        await store.add_favorite(session_id=sid, user_label="alice")

        first = await store.remove_favorite(
            session_id=sid, user_label="alice"
        )
        assert first is True

        second = await store.remove_favorite(
            session_id=sid, user_label="alice"
        )
        assert second is False, (
            "second remove_favorite must return False (no row matched)"
        )

        # Removing a pair that never existed is similarly a no-op.
        ghost = await store.remove_favorite(
            session_id=sid, user_label="never-starred"
        )
        assert ghost is False


class TestListFavorites:
    async def test_list_favorites_ordered_recent_first(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        """Three favorites seeded with spaced ``created_at`` round-trip
        newest-first; each row carries the joined session payload."""
        seeded: list[str] = []
        for i in range(3):
            sid = await _seed_session(
                engine, sid=f"sid-fav-{i:02d}", prompt=f"prompt {i}"
            )
            seeded.append(sid)
            await store.add_favorite(session_id=sid, user_label="alice")
            await asyncio.sleep(0.01)

        rows = await store.list_favorites(user_label="alice")
        assert [r["session_id"] for r in rows] == [
            seeded[2],
            seeded[1],
            seeded[0],
        ]
        assert rows[0]["session"]["spec_json"]["prompt"] == "prompt 2"
        ts = [r["starred_at"] for r in rows]

        def _naive(dt):
            return dt.replace(tzinfo=None) if dt.tzinfo else dt

        assert _naive(ts[0]) > _naive(ts[1]) > _naive(ts[2])

    async def test_list_favorites_other_user_returns_empty(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        """Alice's stars must not bleed into bob's favorites view."""
        sid = await _seed_session(engine)
        await store.add_favorite(session_id=sid, user_label="alice")

        bob = await store.list_favorites(user_label="bob")
        assert bob == []


class TestCascadeDelete:
    async def test_cascade_delete_with_session(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        """Deleting the parent session cascades to its favorite rows.

        SQLite enforces ``ON DELETE CASCADE`` only when
        ``PRAGMA foreign_keys=ON``; ``make_async_engine`` enables
        that pragma on connect. We also set it explicitly on the
        belt-and-braces connection here so the cascade is observable
        even on the shortest-lived connections.
        """
        sid = await _seed_session(engine, sid="sid-cascade")
        added = await store.add_favorite(
            session_id=sid, user_label="alice"
        )
        assert added is True

        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys=ON"))
            await conn.execute(
                sessions.delete().where(sessions.c.id == sid)
            )

        async with engine.connect() as conn:
            await conn.execute(text("PRAGMA foreign_keys=ON"))
            remaining = (
                await conn.execute(
                    session_favorites.select().where(
                        session_favorites.c.session_id == sid
                    )
                )
            ).all()
        assert remaining == [], (
            "cascade delete did not propagate to session_favorites"
        )


class TestIsFavorited:
    async def test_is_favorited_reflects_state(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        """Cheap-lookup helper flips True on add and False on remove."""
        sid = await _seed_session(engine)
        assert await store.is_favorited(
            session_id=sid, user_label="alice"
        ) is False

        await store.add_favorite(session_id=sid, user_label="alice")
        assert await store.is_favorited(
            session_id=sid, user_label="alice"
        ) is True

        assert await store.is_favorited(
            session_id=sid, user_label="bob"
        ) is False

        await store.remove_favorite(session_id=sid, user_label="alice")
        assert await store.is_favorited(
            session_id=sid, user_label="alice"
        ) is False
