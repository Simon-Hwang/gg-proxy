"""Unit tests for SqlAlchemyStore cost aggregation — Plan 8 D8.30 / Task 23.

Exercises the new :meth:`SqlAlchemyStore.aggregate_cost_by_owner` /
:meth:`list_sessions_with_cost` / :meth:`summary_for_user` surface.
Fixture pattern mirrors :mod:`tests.unit.store.test_favorites_repository`
— a fresh on-disk SQLite per test under ``tmp_path`` so multiple
connections behave like production.

Tests:

  * ``test_aggregate_cost_by_owner_groups_correctly`` — three sessions
    for alice + two for bob round-trip with correct count + sum.
  * ``test_summary_for_user_this_month`` — single-user summary
    includes the right ``from_ts`` window start + correct totals.
  * ``test_aggregate_order_by_sessions_vs_cost`` — ``order_by``
    flips the row order between cost-DESC and sessions-DESC even
    when the two orderings disagree (alice has more sessions, bob
    has more cost).
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
    sessions,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncEngine:
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/cost.db")
    await create_all_tables(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def store(engine: AsyncEngine) -> SqlAlchemyStore:
    return SqlAlchemyStore(engine)


async def _seed(
    engine: AsyncEngine,
    sid: str,
    *,
    owner: str | None,
    cost: float,
    submitted_at: datetime | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            sessions.insert().values(
                id=sid,
                status="completed",
                spec_json={"prompt": "p"},
                tags=[],
                submitted_at=submitted_at or datetime.now(UTC),
                backend="inprocess",
                owner=owner,
                cost_usd=cost,
            )
        )


class TestAggregateCostByOwner:
    async def test_aggregate_cost_by_owner_groups_correctly(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        """Three sessions for alice (1.5 USD total) + two for bob
        (5.0 USD total) round-trip with correct count and sum.

        Default ``order_by='cost'`` puts the higher-cost owner first
        — bob (5.0) before alice (1.5) — verifying both the GROUP
        BY arithmetic AND the default ordering at once.
        """
        await _seed(engine, "s1", owner="alice", cost=0.5)
        await _seed(engine, "s2", owner="alice", cost=0.5)
        await _seed(engine, "s3", owner="alice", cost=0.5)
        await _seed(engine, "s4", owner="bob", cost=2.5)
        await _seed(engine, "s5", owner="bob", cost=2.5)

        rows = await store.aggregate_cost_by_owner()
        assert len(rows) == 2
        # Default ordering: cost DESC → bob (5.0) before alice (1.5).
        assert rows[0]["owner"] == "bob"
        assert rows[0]["session_count"] == 2
        assert rows[0]["total_cost_usd"] == pytest.approx(5.0)
        assert rows[1]["owner"] == "alice"
        assert rows[1]["session_count"] == 3
        assert rows[1]["total_cost_usd"] == pytest.approx(1.5)


class TestSummaryForUser:
    async def test_summary_for_user_this_month(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        """Single-user summary returns correct count + cost over
        the ``this_month`` window and echoes the period start in
        the response.

        Two sessions land in the current month + one stale row
        from 60 days ago that MUST NOT be summed in.
        """
        now = datetime.now(UTC)
        await _seed(engine, "this-1", owner="alice", cost=1.0, submitted_at=now)
        await _seed(engine, "this-2", owner="alice", cost=2.0, submitted_at=now)
        # Far-back row, outside any reasonable "this_month" window.
        old = now - timedelta(days=60)
        await _seed(engine, "stale", owner="alice", cost=99.0, submitted_at=old)

        summary = await store.summary_for_user(
            user_label="alice", period="this_month"
        )
        assert summary["user"] == "alice"
        assert summary["period"] == "this_month"
        assert summary["session_count"] == 2
        assert summary["total_cost_usd"] == pytest.approx(3.0)
        # ``from_ts`` is the first-of-month UTC at midnight — confirm
        # it round-trips through ``isoformat`` rather than checking
        # exact wall-clock equality (which would race the test clock).
        ts = datetime.fromisoformat(summary["from_ts"])
        assert ts.day == 1
        assert ts.hour == 0 and ts.minute == 0


class TestAggregateOrdering:
    async def test_aggregate_order_by_sessions_vs_cost(
        self, store: SqlAlchemyStore, engine: AsyncEngine
    ) -> None:
        """``order_by`` flips the ordering when count and cost
        disagree: alice has MORE sessions but LESS cost than bob.

          * ``order_by='cost'``     → bob (10.0, 1 row) first.
          * ``order_by='sessions'`` → alice (3.0, 3 rows) first.
          * ``order_by='owner'``    → alice (alphabetical) first.
        """
        await _seed(engine, "a1", owner="alice", cost=1.0)
        await _seed(engine, "a2", owner="alice", cost=1.0)
        await _seed(engine, "a3", owner="alice", cost=1.0)
        await _seed(engine, "b1", owner="bob", cost=10.0)

        by_cost = await store.aggregate_cost_by_owner(order_by="cost")
        assert [r["owner"] for r in by_cost] == ["bob", "alice"]

        by_sessions = await store.aggregate_cost_by_owner(order_by="sessions")
        assert [r["owner"] for r in by_sessions] == ["alice", "bob"]

        by_owner = await store.aggregate_cost_by_owner(order_by="owner")
        assert [r["owner"] for r in by_owner] == ["alice", "bob"]
