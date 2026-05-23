"""Unit tests for the async SessionRepository + schema.

Each test uses a fresh in-memory SQLite database via the ``repo`` fixture so
the tests are fully isolated and can run in parallel.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.store import (
    SessionRepository,
    create_all_tables,
    frames,
    make_async_engine,
    sessions,
)


@pytest_asyncio.fixture
async def engine(tmp_path) -> AsyncEngine:
    """Fresh on-disk SQLite per test.

    A temp-file is used instead of ``:memory:`` because pooled connections
    each get their own ``:memory:`` database — and ``StaticPool`` causes
    transaction interleave bugs when the bg task and foreground polling
    share a single connection. A scratch file is simple and reliable.
    """
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/relay.db")
    await create_all_tables(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def repo(engine: AsyncEngine) -> SessionRepository:
    return SessionRepository(engine)


# ── sessions CRUD ─────────────────────────────────────────────────────────


class TestSessionsCrud:
    async def test_create_and_get_session(self, repo: SessionRepository):
        await repo.create_session(
            id="s1",
            spec_json={"prompt": "***REDACTED***"},
            trace_id="t-1",
            backend="inprocess",
        )
        row = await repo.get_session("s1")
        assert row is not None
        assert row["id"] == "s1"
        assert row["status"] == "queued"
        assert row["spec_json"] == {"prompt": "***REDACTED***"}
        assert row["backend"] == "inprocess"
        assert row["trace_id"] == "t-1"

    async def test_update_session_status_transitions(
        self, repo: SessionRepository
    ):
        await repo.create_session(
            id="s2", spec_json={}, trace_id=None, backend="docker"
        )
        now = datetime.now(UTC)
        await repo.update_session_status(
            "s2",
            status="running",
            started_at=now,
            runtime_id="runtime-abc",
        )
        row = await repo.get_session("s2")
        assert row["status"] == "running"
        assert row["runtime_id"] == "runtime-abc"
        assert row["started_at"] is not None

        await repo.update_session_status(
            "s2",
            status="completed",
            ended_at=now + timedelta(seconds=5),
            end_reason="ok",
        )
        row = await repo.get_session("s2")
        assert row["status"] == "completed"
        assert row["end_reason"] == "ok"

    async def test_list_sessions_filter_by_status(
        self, repo: SessionRepository
    ):
        for i in range(3):
            await repo.create_session(
                id=f"q{i}", spec_json={}, trace_id=None, backend="inprocess"
            )
        await repo.update_session_status("q0", status="running")
        rows, _next = await repo.list_sessions(status="queued")
        assert {r["id"] for r in rows} == {"q1", "q2"}
        rows_running, _next = await repo.list_sessions(status="running")
        assert {r["id"] for r in rows_running} == {"q0"}


# ── frames ────────────────────────────────────────────────────────────────


class TestFramesCrud:
    async def test_append_and_list_frames(self, repo: SessionRepository):
        await repo.create_session(
            id="sf", spec_json={}, trace_id=None, backend="inprocess"
        )
        ts = datetime.now(UTC)
        for seq in range(1, 4):
            await repo.append_frame(
                "sf", seq=seq, ts=ts, type_="msg.chunk", payload={"i": seq}
            )
        rows = await repo.list_frames("sf")
        assert [r["seq"] for r in rows] == [1, 2, 3]
        assert rows[0]["payload"] == {"i": 1}

    async def test_unique_seq_per_session(self, repo: SessionRepository):
        await repo.create_session(
            id="su", spec_json={}, trace_id=None, backend="inprocess"
        )
        ts = datetime.now(UTC)
        await repo.append_frame(
            "su", seq=1, ts=ts, type_="msg.chunk", payload={}
        )
        with pytest.raises(IntegrityError):
            await repo.append_frame(
                "su", seq=1, ts=ts, type_="msg.chunk", payload={}
            )

    async def test_list_frames_pagination(self, repo: SessionRepository):
        await repo.create_session(
            id="sp", spec_json={}, trace_id=None, backend="inprocess"
        )
        ts = datetime.now(UTC)
        for s in range(10):
            await repo.append_frame(
                "sp", seq=s, ts=ts, type_="msg.chunk", payload={"i": s}
            )
        page1 = await repo.list_frames("sp", limit=3, offset=0)
        page2 = await repo.list_frames("sp", limit=3, offset=3)
        assert [r["seq"] for r in page1] == [0, 1, 2]
        assert [r["seq"] for r in page2] == [3, 4, 5]


# ── hitl ─────────────────────────────────────────────────────────────────


class TestHitlCrud:
    async def test_upsert_and_get_hitl(self, repo: SessionRepository):
        await repo.create_session(
            id="sh", spec_json={}, trace_id=None, backend="inprocess"
        )
        await repo.upsert_hitl(
            id="sh:abc",
            session_id="sh",
            tool="Bash",
            args_json={"cmd": "ls"},
            status="pending",
        )
        row = await repo.get_hitl("sh:abc")
        assert row is not None
        assert row["status"] == "pending"

        await repo.upsert_hitl(
            id="sh:abc",
            session_id="sh",
            tool="Bash",
            args_json={"cmd": "ls"},
            status="accepted",
            resolved_at=datetime.now(UTC),
            resolver="admin",
        )
        row = await repo.get_hitl("sh:abc")
        assert row["status"] == "accepted"
        assert row["resolver"] == "admin"

    async def test_list_pending_hitl(self, repo: SessionRepository):
        await repo.create_session(
            id="shp", spec_json={}, trace_id=None, backend="inprocess"
        )
        await repo.upsert_hitl(
            id="shp:r1",
            session_id="shp",
            tool="Bash",
            args_json={},
            status="pending",
        )
        await repo.upsert_hitl(
            id="shp:r2",
            session_id="shp",
            tool="Bash",
            args_json={},
            status="accepted",
        )
        pending = await repo.list_pending_hitl(session_id="shp")
        assert {r["id"] for r in pending} == {"shp:r1"}


# ── retention + recovery ─────────────────────────────────────────────────


class TestRetentionAndRecovery:
    async def test_prune_frames_older_than(self, repo: SessionRepository):
        await repo.create_session(
            id="sr", spec_json={}, trace_id=None, backend="inprocess"
        )
        old = datetime.now(UTC) - timedelta(days=40)
        new = datetime.now(UTC)
        await repo.append_frame(
            "sr", seq=1, ts=old, type_="msg.chunk", payload={}
        )
        await repo.append_frame(
            "sr", seq=2, ts=new, type_="msg.chunk", payload={}
        )
        cutoff = datetime.now(UTC) - timedelta(days=30)
        deleted = await repo.prune_frames_older_than(cutoff=cutoff)
        assert deleted == 1
        remaining = await repo.list_frames("sr")
        assert [r["seq"] for r in remaining] == [2]

    async def test_mark_in_flight_only_touches_running(
        self, repo: SessionRepository
    ):
        await repo.create_session(
            id="r1", spec_json={}, trace_id=None, backend="inprocess"
        )
        await repo.create_session(
            id="r2", spec_json={}, trace_id=None, backend="inprocess"
        )
        await repo.create_session(
            id="r3", spec_json={}, trace_id=None, backend="inprocess"
        )
        await repo.update_session_status("r1", status="running")
        await repo.update_session_status("r2", status="completed")
        ids = await repo.mark_in_flight_as_interrupted()
        assert ids == ["r1"]
        row1 = await repo.get_session("r1")
        row2 = await repo.get_session("r2")
        row3 = await repo.get_session("r3")
        assert row1["status"] == "interrupted"
        assert row1["end_reason"] == "interrupted_on_startup"
        assert row1["ended_at"] is not None
        assert row2["status"] == "completed"
        assert row3["status"] == "queued"

        ids2 = await repo.mark_in_flight_as_interrupted()
        assert ids2 == []

    async def test_cascade_delete_removes_children(
        self, repo: SessionRepository, engine: AsyncEngine
    ):
        from sqlalchemy import select as _sel

        await repo.create_session(
            id="cd", spec_json={}, trace_id=None, backend="inprocess"
        )
        await repo.append_frame(
            "cd",
            seq=1,
            ts=datetime.now(UTC),
            type_="msg.chunk",
            payload={},
        )
        await repo.upsert_hitl(
            id="cd:x",
            session_id="cd",
            tool="Bash",
            args_json={},
            status="pending",
        )
        # Enable FK enforcement for SQLite within this test connection.
        async with engine.begin() as conn:
            from sqlalchemy import text

            await conn.execute(text("PRAGMA foreign_keys=ON"))
            await conn.execute(sessions.delete().where(sessions.c.id == "cd"))
        async with engine.connect() as conn:
            n_frames = (
                await conn.execute(
                    _sel(frames).where(frames.c.session_id == "cd")
                )
            ).all()
        assert n_frames == []

    async def test_transactional_rollback_on_error(
        self, repo: SessionRepository
    ):
        """A failing INSERT must not leave a half-written session row behind."""
        await repo.create_session(
            id="tx", spec_json={}, trace_id=None, backend="inprocess"
        )
        with pytest.raises(IntegrityError):
            # Duplicate id violates PK
            await repo.create_session(
                id="tx", spec_json={}, trace_id=None, backend="inprocess"
            )
        rows, _next = await repo.list_sessions()
        assert sum(1 for r in rows if r["id"] == "tx") == 1
