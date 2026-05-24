"""Retention prune behaviour — Plan 8 Task 20 / D8.3.

Covers the four documented invariants:

* ``--dry-run`` reports counts but does **not** delete.
* A live run removes rows past their cutoff and preserves recent rows.
* Batching survives a row count >> ``batch_size`` and reports
  ``batches`` correctly.
* ``hitl_requests`` rows with ``resolved_at IS NULL`` are preserved
  even if their ``created_at`` is ancient — operators care more
  about unresolved HITL than the strict 30-day window.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from gg_relay.maintenance.retention import run_retention
from gg_relay.store.engine import create_all_tables, make_async_engine
from gg_relay.store.schema import audit_log, events, hitl_requests

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def engine(tmp_path):
    db_file = tmp_path / "retention.db"
    eng = make_async_engine(f"sqlite+aiosqlite:///{db_file}")
    try:
        await create_all_tables(eng)
        yield eng
    finally:
        await eng.dispose()


async def _insert_event(eng, *, event_id: str, ts: datetime) -> None:
    async with eng.begin() as conn:
        await conn.execute(
            events.insert().values(
                event_id=event_id,
                ts=ts,
                type="TestEvent",
                session_id=None,
                payload={"x": 1},
                delivery_tier="disk",
            )
        )


async def _insert_audit(eng, *, ts: datetime, actor: str = "alice") -> None:
    async with eng.begin() as conn:
        await conn.execute(
            audit_log.insert().values(
                ts=ts,
                actor=actor,
                action="probe",
                target_type=None,
                target_id=None,
                metadata_json=None,
                request_id=None,
            )
        )


async def _insert_hitl(
    eng,
    *,
    sid: str,
    hid: str,
    created_at: datetime,
    resolved_at: datetime | None,
) -> None:
    async with eng.begin() as conn:
        from gg_relay.store.schema import sessions

        existing = await conn.execute(
            sessions.select().where(sessions.c.id == sid)
        )
        if existing.first() is None:
            await conn.execute(
                sessions.insert().values(
                    id=sid,
                    status="completed",
                    spec_json={},
                    tags=[],
                    submitted_at=created_at,
                    started_at=created_at,
                    ended_at=resolved_at,
                    end_reason=None,
                    trace_id=None,
                    backend="inprocess",
                    runtime_id=None,
                )
            )
        await conn.execute(
            hitl_requests.insert().values(
                id=hid,
                session_id=sid,
                tool="probe",
                args_json={},
                status="resolved" if resolved_at else "pending",
                created_at=created_at,
                resolved_at=resolved_at,
                reason=None,
                resolver=None,
            )
        )


async def _count(eng, table) -> int:
    async with eng.connect() as conn:
        result = await conn.execute(table.select())
        return len(result.all())


async def test_dry_run_reports_counts_without_deleting(engine) -> None:
    now = datetime.now(UTC)
    # audit_log default retention is 90 days — pick a horizon past
    # both events (30d) and audit_log (90d) so the preview reports
    # ≥ 1 row per table.
    old = now - timedelta(days=120)
    await _insert_event(engine, event_id="e-old", ts=old)
    await _insert_event(engine, event_id="e-new", ts=now)
    await _insert_audit(engine, ts=old)
    await _insert_audit(engine, ts=now)

    result = await run_retention(engine=engine, dry_run=True)

    assert result.dry_run is True
    assert result.total_deleted >= 2
    by_table = {s.table: s for s in result.summaries}
    assert by_table["events"].rows_deleted == 1
    assert by_table["audit_log"].rows_deleted == 1
    assert all(s.dry_run for s in result.summaries)
    assert await _count(engine, events) == 2
    assert await _count(engine, audit_log) == 2


async def test_live_run_deletes_old_preserves_recent(engine) -> None:
    now = datetime.now(UTC)
    ancient = now - timedelta(days=200)
    recent = now - timedelta(hours=1)
    await _insert_event(engine, event_id="e-ancient", ts=ancient)
    await _insert_event(engine, event_id="e-recent", ts=recent)
    await _insert_audit(engine, ts=ancient)
    await _insert_audit(engine, ts=recent)

    result = await run_retention(engine=engine, dry_run=False)

    assert result.dry_run is False
    by_table = {s.table: s for s in result.summaries}
    assert by_table["events"].rows_deleted == 1
    assert by_table["audit_log"].rows_deleted == 1
    assert await _count(engine, events) == 1
    assert await _count(engine, audit_log) == 1


async def test_batched_delete_handles_more_than_batch_size(engine) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=200)
    for i in range(25):
        await _insert_event(engine, event_id=f"e-{i:03d}", ts=old)

    result = await run_retention(
        engine=engine, dry_run=False, batch_size=10
    )

    by_table = {s.table: s for s in result.summaries}
    assert by_table["events"].rows_deleted == 25
    assert by_table["events"].batches >= 3
    assert await _count(engine, events) == 0


async def test_hitl_unresolved_rows_preserved(engine) -> None:
    now = datetime.now(UTC)
    ancient = now - timedelta(days=200)
    await _insert_hitl(
        engine,
        sid="sid-A",
        hid="hid-resolved",
        created_at=ancient,
        resolved_at=ancient,
    )
    await _insert_hitl(
        engine,
        sid="sid-B",
        hid="hid-pending",
        created_at=ancient,
        resolved_at=None,
    )

    result = await run_retention(engine=engine, dry_run=False)

    by_table = {s.table: s for s in result.summaries}
    assert by_table["hitl_requests"].rows_deleted == 1
    async with engine.connect() as conn:
        remaining = (
            await conn.execute(hitl_requests.select())
        ).all()
    assert len(remaining) == 1
    assert remaining[0]._mapping["id"] == "hid-pending"
