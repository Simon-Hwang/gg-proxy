"""Plan 9 D9.9 — events.seq + fetch_after tests.

Exercises:

1. The ``events.seq`` column (Alembic 0012) is NOT NULL with a
   unique index — confirms the simplified single-step migration.
2. ``SqlAlchemyDurableEventStore.persist`` against a SQLite engine
   stamps a strictly-monotonic per-row seq via the INSERT...SELECT
   COALESCE(MAX(seq),0)+1 path inside ``engine.begin()``.
3. ``fetch_after`` returns events ordered by seq (the only cursor
   format post-simplification) with the new row-seq semantics.
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
    """Fresh in-memory SQLite engine with the full schema applied."""
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
        assert "seq" in cols, "Alembic 0012 should add events.seq"

    def test_seq_column_is_not_nullable(self) -> None:
        """Post-simplification: seq is NOT NULL from creation."""
        assert events.c.seq.nullable is False


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


class TestFetchAfter:
    """D9.9 — fetch_after walks the seq column."""

    @pytest.mark.asyncio
    async def test_returns_events_with_seq_greater_than_cursor(
        self, sqlite_engine
    ) -> None:
        store = SqlAlchemyDurableEventStore(sqlite_engine)
        for i in range(5):
            await store.persist(_make_event(f"s{i}"))
        rows = await store.fetch_after(last_seq=2)
        assert len(rows) == 3
        seqs = [getattr(r, "seq", None) for r in rows]
        assert seqs == [3, 4, 5]

    @pytest.mark.asyncio
    async def test_cursor_zero_returns_all_events(self, sqlite_engine) -> None:
        store = SqlAlchemyDurableEventStore(sqlite_engine)
        for i in range(3):
            await store.persist(_make_event(f"s{i}"))
        rows = await store.fetch_after(last_seq=0)
        assert len(rows) == 3

    @pytest.mark.asyncio
    async def test_limit_caps_rowcount(self, sqlite_engine) -> None:
        store = SqlAlchemyDurableEventStore(sqlite_engine)
        for i in range(10):
            await store.persist(_make_event(f"s{i}"))
        rows = await store.fetch_after(last_seq=0, limit=4)
        assert len(rows) == 4
        seqs = [getattr(r, "seq", None) for r in rows]
        assert seqs == [1, 2, 3, 4]


class TestInMemoryFetchAfter:
    """InMemoryDurableEventStore parity — single fetch_after method."""

    @pytest.mark.asyncio
    async def test_in_memory_fetch_after_uses_seq(self) -> None:
        store = InMemoryDurableEventStore()
        await store.persist(_make_event("s1"))
        await store.persist(_make_event("s2"))
        events_list = list(await store.fetch_after(last_seq=0))
        assert len(events_list) == 2
        # Cursor at seq=1 → only seq=2 returned.
        rest = list(await store.fetch_after(last_seq=1))
        assert len(rest) == 1
