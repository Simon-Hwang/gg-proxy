"""recover_on_startup tests — Plan 4 Task 6."""
from __future__ import annotations

import pytest_asyncio

from gg_relay.session.recovery import RecoveryReport, recover_on_startup
from gg_relay.store import SessionRepository, create_all_tables, make_async_engine


@pytest_asyncio.fixture
async def store(tmp_path) -> SessionRepository:
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/_rec.db")
    await create_all_tables(eng)
    yield SessionRepository(eng)
    await eng.dispose()


class TestRecoveryOnStartup:
    async def test_no_running_sessions_no_op(self, store: SessionRepository):
        report = await recover_on_startup(store)
        assert report == RecoveryReport(0, ())

    async def test_in_flight_marked_interrupted(self, store: SessionRepository):
        await store.create_session(
            id="a", spec_json={}, trace_id=None, backend="inprocess"
        )
        await store.create_session(
            id="b", spec_json={}, trace_id=None, backend="docker"
        )
        await store.update_session_status("a", status="running")
        await store.update_session_status("b", status="running")
        report = await recover_on_startup(store)
        assert report.interrupted_count == 2
        assert set(report.interrupted_ids) == {"a", "b"}
        row_a = await store.get_session("a")
        assert row_a["status"] == "interrupted"
        assert row_a["end_reason"] == "interrupted_on_startup"
        assert row_a["ended_at"] is not None

    async def test_idempotent_re_run(self, store: SessionRepository):
        await store.create_session(
            id="a", spec_json={}, trace_id=None, backend="inprocess"
        )
        await store.update_session_status("a", status="running")
        first = await recover_on_startup(store)
        second = await recover_on_startup(store)
        assert first.interrupted_count == 1
        assert second == RecoveryReport(0, ())

    async def test_non_running_status_untouched(
        self, store: SessionRepository
    ):
        await store.create_session(
            id="q", spec_json={}, trace_id=None, backend="inprocess"
        )
        await store.create_session(
            id="d", spec_json={}, trace_id=None, backend="inprocess"
        )
        await store.update_session_status("d", status="completed")
        report = await recover_on_startup(store)
        assert report.interrupted_count == 0
        qrow = await store.get_session("q")
        drow = await store.get_session("d")
        assert qrow["status"] == "queued"
        assert drow["status"] == "completed"
