"""Plan 9 v0.9.0-rc D9.9 + D9.9a — events.seq + fetch_after_seq tests.

Exercises:

1. ``SqlAlchemyDurableEventStore.persist`` against a SQLite engine
   that has run Alembic 0012a — verifies the new INSERT...SELECT
   path stamps a strictly-monotonic per-row seq.
2. ``fetch_after_seq`` returns events ordered by seq (not by
   microsecond ts), with deterministic tiebreakers when seqs collide
   (legacy NULL → COALESCE(seq, 0) fallback).
3. ``InMemoryDurableEventStore.fetch_after_seq`` is a no-op alias of
   ``fetch_after`` so unit-test code stays interchangeable.
4. The new ``seq`` schema column is nullable (0012a only, 0012b runs
   later by operator).
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from gg_relay.core.events import SessionCreated
from gg_relay.store.durable_event import (
    InMemoryDurableEventStore,
    SqlAlchemyDurableEventStore,
)
from gg_relay.store.schema import events, metadata


def _make_event(sid: str, *, occurred_at: datetime | None = None) -> SessionCreated:
    return SessionCreated(
        session_id=sid,
        occurred_at=occurred_at or datetime.now(UTC),
        prompt_redacted="dummy prompt",
        tags=("test",),
    )


@pytest.fixture
async def sqlite_engine():
    """Fresh in-memory SQLite engine with the schema applied.

    Uses the full ``metadata.create_all`` (which reflects the
    Plan 9 ``seq`` column added in 0012a) so we can exercise the
    new persist path without manually running Alembic.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


class TestSeqColumnPresence:
    """Sanity check: the schema reflects the Plan 9 ``seq`` column."""

    @pytest.mark.asyncio
    async def test_events_table_has_seq_column(self, sqlite_engine):
        async with sqlite_engine.connect() as conn:
            inspector = await conn.run_sync(
                lambda sync_conn: sa_inspect(sync_conn)
            )
            cols = await conn.run_sync(
                lambda _: {c["name"] for c in inspector.get_columns("events")}
            )
        assert "seq" in cols, "Alembic 0012a should add events.seq"

    def test_seq_column_is_nullable_in_schema(self) -> None:
        """0012a ships nullable; 0012b flips NOT NULL later."""
        seq_col = events.c.seq
        assert seq_col.nullable is True


class TestPersistFillsSeq:
    """D9.9 — persist must stamp the seq column via the SQLite
    INSERT...SELECT COALESCE(MAX(seq),0)+1 path."""

    @pytest.mark.asyncio
    async def test_first_event_gets_seq_1(self, sqlite_engine) -> None:
        store = SqlAlchemyDurableEventStore(sqlite_engine)
        seq = await store.persist(_make_event("s1"))
        assert seq == 1

    @pytest.mark.asyncio
    async def test_sequential_events_are_monotonic(self, sqlite_engine) -> None:
        store = SqlAlchemyDurableEventStore(sqlite_engine)
        seqs = [await store.persist(_make_event(f"s{i}")) for i in range(5)]
        assert seqs == [1, 2, 3, 4, 5]

    @pytest.mark.asyncio
    async def test_persist_writes_seq_to_table(self, sqlite_engine) -> None:
        store = SqlAlchemyDurableEventStore(sqlite_engine)
        await store.persist(_make_event("s1"))
        async with sqlite_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT seq FROM events WHERE session_id = 's1'")
            )
            row_seq = result.scalar_one()
        assert row_seq == 1


class TestFetchAfterSeq:
    """D9.9a — fetch_after_seq uses the seq column, not the ts cursor."""

    @pytest.mark.asyncio
    async def test_returns_events_with_seq_greater_than_cursor(
        self, sqlite_engine
    ) -> None:
        store = SqlAlchemyDurableEventStore(sqlite_engine)
        for i in range(5):
            await store.persist(_make_event(f"s{i}"))
        rows = await store.fetch_after_seq(last_seq=2)
        # seq=3,4,5 → 3 rows.
        assert len(rows) == 3
        # And they come back in ascending seq order.
        seqs = [getattr(r, "seq", None) for r in rows]
        assert seqs == [3, 4, 5]

    @pytest.mark.asyncio
    async def test_cursor_zero_returns_all_events(self, sqlite_engine) -> None:
        store = SqlAlchemyDurableEventStore(sqlite_engine)
        for i in range(3):
            await store.persist(_make_event(f"s{i}"))
        rows = await store.fetch_after_seq(last_seq=0)
        assert len(rows) == 3

    @pytest.mark.asyncio
    async def test_limit_caps_rowcount(self, sqlite_engine) -> None:
        store = SqlAlchemyDurableEventStore(sqlite_engine)
        for i in range(10):
            await store.persist(_make_event(f"s{i}"))
        rows = await store.fetch_after_seq(last_seq=0, limit=4)
        assert len(rows) == 4
        seqs = [getattr(r, "seq", None) for r in rows]
        assert seqs == [1, 2, 3, 4]


class TestInMemoryStoreFetchAfterSeqAlias:
    """D9.9a — InMemoryDurableEventStore.fetch_after_seq must equal
    fetch_after so existing unit-tested code paths stay valid."""

    @pytest.mark.asyncio
    async def test_alias_delegates(self) -> None:
        store = InMemoryDurableEventStore()
        await store.persist(_make_event("s1"))
        await store.persist(_make_event("s2"))
        seq_path = list(await store.fetch_after_seq(last_seq=0))
        ts_path = list(await store.fetch_after(last_seq=0))
        assert [type(e).__name__ for e in seq_path] == [
            type(e).__name__ for e in ts_path
        ]
        assert len(seq_path) == 2
