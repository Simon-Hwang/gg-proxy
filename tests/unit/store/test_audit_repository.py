"""Unit tests for SqlAlchemyStore audit methods — Plan 8 D8.4 / Task 5.

Exercises the new :meth:`SqlAlchemyStore.record_audit` +
:meth:`list_audit` surface:

* ``test_record_audit_returns_id`` — write returns the new row's id
  > 0 and the row materialises in the table.
* ``test_list_audit_filter_by_session`` — ``session_id=`` convenience
  alias resolves to ``target_type='session'`` + ``target_id=<sid>``
  and excludes unrelated rows.
* ``test_list_audit_cursor_pagination`` — 100 rows seeded + paged
  through with ``limit=50`` produces two complete pages and an
  exhausted (``next_cursor is None``) third.
* ``test_record_audit_within_transaction`` — passing ``conn=`` to
  :meth:`record_audit` inside an ``engine.begin()`` block writes
  inside the same transaction (visible after commit; rolled back
  together with the surrounding mutation on exception).

The fixture pattern matches :mod:`tests.unit.store.test_cursor_pagination`
— a fresh on-disk SQLite per test under ``tmp_path`` so paging
behaves like production where multiple connections may serve a
single page.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.store import (
    SqlAlchemyStore,
    audit_log,
    create_all_tables,
    make_async_engine,
)

pytestmark = pytest.mark.asyncio


# ── fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncEngine:
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/audit.db")
    await create_all_tables(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def store(engine: AsyncEngine) -> SqlAlchemyStore:
    return SqlAlchemyStore(engine)


# ── record_audit ──────────────────────────────────────────────────────


class TestRecordAudit:
    async def test_record_audit_returns_id(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        """Write returns the new row's id and the row materialises."""
        row_id = await store.record_audit(
            actor="alice",
            action="session_create",
            target_type="session",
            target_id="sid-xyz",
            metadata={"backend": "inprocess"},
            request_id="req-001",
        )
        assert isinstance(row_id, int) and row_id > 0

        # Verify the row materialised with all fields.
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    audit_log.select().where(audit_log.c.id == row_id)
                )
            ).mappings().first()
        assert row is not None
        assert row["actor"] == "alice"
        assert row["action"] == "session_create"
        assert row["target_type"] == "session"
        assert row["target_id"] == "sid-xyz"
        assert row["metadata_json"] == {"backend": "inprocess"}
        assert row["request_id"] == "req-001"
        assert row["ts"] is not None

    async def test_record_audit_with_explicit_ts(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        """Caller-supplied ts wins over the auto-generated default."""
        anchor = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        row_id = await store.record_audit(
            actor="bob",
            action="session_cancel",
            ts=anchor,
        )
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    audit_log.select().where(audit_log.c.id == row_id)
                )
            ).mappings().first()
        assert row is not None
        # SQLite returns naive datetime; compare via isoformat.
        recorded = row["ts"]
        # SQLAlchemy may return tz-aware or naive depending on dialect.
        if recorded.tzinfo is None:
            assert recorded == anchor.replace(tzinfo=None)
        else:
            assert recorded == anchor


# ── list_audit (filtering + pagination) ───────────────────────────────


class TestListAudit:
    async def test_list_audit_filter_by_session(
        self, store: SqlAlchemyStore
    ) -> None:
        """``session_id=sid`` alias resolves to target_type+target_id."""
        # Seed: 3 audit rows for sid-A, 2 for sid-B, 1 for non-session.
        for action in ("session_create", "session_pause", "session_resume"):
            await store.record_audit(
                actor="alice",
                action=action,
                target_type="session",
                target_id="sid-A",
            )
        for action in ("session_create", "session_cancel"):
            await store.record_audit(
                actor="bob",
                action=action,
                target_type="session",
                target_id="sid-B",
            )
        await store.record_audit(
            actor="carol",
            action="hitl_approve",
            target_type="hitl",
            target_id="h-001",
        )

        rows_a, nxt_a = await store.list_audit(session_id="sid-A")
        assert nxt_a is None
        assert len(rows_a) == 3
        assert {r["action"] for r in rows_a} == {
            "session_create",
            "session_pause",
            "session_resume",
        }
        assert all(r["target_id"] == "sid-A" for r in rows_a)

        rows_b, nxt_b = await store.list_audit(session_id="sid-B")
        assert nxt_b is None
        assert len(rows_b) == 2
        assert all(r["target_id"] == "sid-B" for r in rows_b)

        # Non-session rows are excluded.
        actions_a_b = {r["action"] for r in (*rows_a, *rows_b)}
        assert "hitl_approve" not in actions_a_b

    async def test_list_audit_cursor_pagination(
        self, store: SqlAlchemyStore
    ) -> None:
        """100 rows → 50 + 50 + exhausted; cursor stable across pages."""
        # Seed 100 rows with strictly-increasing ts so newest-first
        # paging produces a deterministic order. Spaced by 1 second to
        # keep the SQLite datetime resolution from collapsing them.
        base = datetime.now(UTC)
        for i in range(100):
            await store.record_audit(
                actor="alice",
                action="session_create",
                target_type="session",
                target_id=f"sid-{i:03d}",
                ts=base + timedelta(seconds=i),
            )

        # First page: 50 rows + a next cursor.
        page1, cur1 = await store.list_audit(actor="alice", limit=50)
        assert len(page1) == 50
        assert cur1 is not None
        # Newest-first — sid-099 should be on page 1.
        assert page1[0]["target_id"] == "sid-099"

        # Second page: remaining 50, no next cursor.
        page2, cur2 = await store.list_audit(
            actor="alice", limit=50, after=cur1
        )
        assert len(page2) == 50
        assert cur2 is None
        # Oldest in page 2 is sid-000.
        assert page2[-1]["target_id"] == "sid-000"

        # Pages don't overlap.
        ids_page1 = {r["target_id"] for r in page1}
        ids_page2 = {r["target_id"] for r in page2}
        assert ids_page1.isdisjoint(ids_page2)
        assert len(ids_page1 | ids_page2) == 100


# ── same-tx write (durable outbox) ───────────────────────────────────


class TestRecordAuditWithinTransaction:
    async def test_record_audit_within_transaction(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        """Passing ``conn=`` writes inside an externally-managed tx.

        The audit row is visible *after* the surrounding ``begin()``
        block commits, AND a deliberate exception inside the block
        rolls it back along with whatever mutation it accompanied —
        the v2.1 MAJOR 3 durable-outbox guarantee.
        """
        # Happy path: same-tx write commits with the surrounding block.
        async with engine.begin() as conn:
            row_id = await store.record_audit(
                actor="alice",
                action="session_create",
                target_type="session",
                target_id="sid-tx-1",
                conn=conn,
            )
            assert row_id > 0
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    audit_log.select().where(audit_log.c.id == row_id)
                )
            ).mappings().first()
        assert row is not None
        assert row["target_id"] == "sid-tx-1"

        # Rollback path: the audit row written inside a failing tx must
        # NOT survive. We trigger rollback by raising inside
        # ``engine.begin()`` and then check the row is absent.
        with pytest.raises(RuntimeError, match="forced rollback"):
            async with engine.begin() as conn:
                await store.record_audit(
                    actor="bob",
                    action="session_cancel",
                    target_type="session",
                    target_id="sid-tx-2",
                    conn=conn,
                )
                raise RuntimeError("forced rollback")
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    audit_log.select().where(
                        audit_log.c.target_id == "sid-tx-2"
                    )
                )
            ).mappings().first()
        assert row is None, (
            "audit row written via conn= survived a transaction rollback"
        )
