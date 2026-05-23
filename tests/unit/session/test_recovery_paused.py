"""recover_paused_timers tests — Plan 7 D7.18 / Task 14.

Exercises the startup-recovery hook that re-arms paused-timeout
watchdogs after a relay restart. The hook walks every ``paused`` row,
computes ``elapsed = now - paused_at`` and decides:

* ``elapsed > paused_timeout_s`` → cancel the session with
  ``reason='paused_timeout_recovered'``.
* otherwise → re-arm the asyncio watchdog with the remaining window
  via :meth:`SessionManager._arm_paused_timer(remaining_s=...)`.

We use a lightweight fake manager so the tests stay isolated from the
full SessionManager wiring; the fake only implements the surface
exercised by :func:`recover_paused_timers` (the two-method
:class:`_PausedManagerLike` Protocol).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio

from gg_relay.session.recovery import recover_paused_timers
from gg_relay.store import SessionRepository, create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


@dataclass
class _FakeManager:
    """Records calls to the two surfaces the recovery hook needs."""

    cancels: list[tuple[str, str]] = field(default_factory=list)
    rearms: list[tuple[str, float]] = field(default_factory=list)
    cancel_raises: BaseException | None = None
    rearm_raises: BaseException | None = None

    async def cancel(self, sid: str, *, reason: str = "user_request") -> None:
        if self.cancel_raises is not None:
            raise self.cancel_raises
        self.cancels.append((sid, reason))

    def _arm_paused_timer(
        self, sid: str, *, remaining_s: float | None = None
    ) -> None:
        if self.rearm_raises is not None:
            raise self.rearm_raises
        self.rearms.append((sid, float(remaining_s or 0.0)))


@dataclass
class _FakeStore:
    """Returns a pre-built list of rows to drive the recovery walk."""

    rows: Sequence[dict[str, Any]] = field(default_factory=list)

    async def list_paused(self) -> Sequence[dict[str, Any]]:
        return list(self.rows)


def _row(sid: str, *, paused_at: datetime) -> dict[str, Any]:
    return {"id": sid, "paused_at": paused_at, "status": "paused"}


@pytest_asyncio.fixture
async def real_store(tmp_path):
    """Real :class:`SqlAlchemyStore` against an isolated SQLite DB.

    Used by :class:`TestRecoveryWithRealStore` to make sure the
    Protocol method ``list_paused()`` returns the rows recovery
    expects.
    """
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/_rec_paused.db")
    await create_all_tables(eng)
    yield SessionRepository(eng)
    await eng.dispose()


class TestRecoverPausedTimers:
    async def test_recover_paused_under_timeout_rearms(self) -> None:
        now = datetime.now(UTC)
        paused_at = now - timedelta(minutes=1)
        store = _FakeStore(rows=[_row("s1", paused_at=paused_at)])
        manager = _FakeManager()

        rearmed, cancelled = await recover_paused_timers(
            manager, store, paused_timeout_s=1800, now=now
        )

        assert (rearmed, cancelled) == (1, 0)
        assert len(manager.rearms) == 1
        sid, remaining = manager.rearms[0]
        assert sid == "s1"
        # 30 min total - 1 min elapsed ≈ 29 min
        assert 1700.0 < remaining < 1800.0
        assert manager.cancels == []

    async def test_recover_paused_over_timeout_cancels(self) -> None:
        now = datetime.now(UTC)
        paused_at = now - timedelta(minutes=31)
        store = _FakeStore(rows=[_row("s2", paused_at=paused_at)])
        manager = _FakeManager()

        rearmed, cancelled = await recover_paused_timers(
            manager, store, paused_timeout_s=1800, now=now
        )

        assert (rearmed, cancelled) == (0, 1)
        assert manager.cancels == [("s2", "paused_timeout_recovered")]
        assert manager.rearms == []

    async def test_recover_idempotent(self) -> None:
        """Running recovery twice against the same rows yields the same counts.

        In production the second pass would see a different DB state
        (cancelled rows no longer match the paused filter, re-armed
        rows may have moved on), but the recovery function itself
        must be safe to invoke against a stable snapshot — re-arming
        a row twice in a row simply re-creates the timer (the
        manager cancels the pre-existing one).
        """
        now = datetime.now(UTC)
        rows = [
            _row("a", paused_at=now - timedelta(minutes=1)),
            _row("b", paused_at=now - timedelta(minutes=31)),
        ]
        store = _FakeStore(rows=rows)
        manager = _FakeManager()

        first = await recover_paused_timers(
            manager, store, paused_timeout_s=1800, now=now
        )
        second = await recover_paused_timers(
            manager, store, paused_timeout_s=1800, now=now
        )

        assert first == (1, 1)
        assert second == (1, 1)
        # Each pass appends one rearm + one cancel; the manager
        # surface doesn't deduplicate (production manager's
        # ``_arm_paused_timer`` cancels the prior timer itself, but
        # the fake just records every call).
        assert len(manager.rearms) == 2
        assert len(manager.cancels) == 2

    async def test_recover_returns_counts(self) -> None:
        """Mixed batch of 1 rearm + 2 cancels → ``(1, 2)``."""
        now = datetime.now(UTC)
        rows = [
            _row("under", paused_at=now - timedelta(seconds=30)),
            _row("over1", paused_at=now - timedelta(minutes=40)),
            _row("over2", paused_at=now - timedelta(minutes=50)),
        ]
        store = _FakeStore(rows=rows)
        manager = _FakeManager()

        result = await recover_paused_timers(
            manager, store, paused_timeout_s=1800, now=now
        )

        assert result == (1, 2)
        assert manager.rearms[0][0] == "under"
        cancelled_ids = {sid for sid, _ in manager.cancels}
        assert cancelled_ids == {"over1", "over2"}

    async def test_recover_skips_null_paused_at(self) -> None:
        """Defensive: rows with ``paused_at IS NULL`` are silently skipped."""
        now = datetime.now(UTC)
        rows = [
            {"id": "no-ts", "paused_at": None, "status": "paused"},
            _row("good", paused_at=now - timedelta(minutes=1)),
        ]
        store = _FakeStore(rows=rows)
        manager = _FakeManager()

        rearmed, cancelled = await recover_paused_timers(
            manager, store, paused_timeout_s=1800, now=now
        )

        assert (rearmed, cancelled) == (1, 0)
        assert manager.rearms[0][0] == "good"

    async def test_recover_cancel_failure_is_logged_and_continues(self) -> None:
        """A cancel that raises shouldn't break the rest of the batch."""
        now = datetime.now(UTC)
        rows = [
            _row("bad", paused_at=now - timedelta(minutes=40)),
            _row("ok", paused_at=now - timedelta(minutes=50)),
        ]
        store = _FakeStore(rows=rows)
        # Manager that always raises — recovery should swallow and
        # report 0 cancels.
        manager = _FakeManager(cancel_raises=RuntimeError("db down"))

        rearmed, cancelled = await recover_paused_timers(
            manager, store, paused_timeout_s=1800, now=now
        )

        assert (rearmed, cancelled) == (0, 0)


class TestRecoveryWithRealStore:
    """Smoke-check :meth:`SqlAlchemyStore.list_paused` matches the
    fixture rows the recovery hook expects.

    The DB-level integration test (full SessionManager pipeline) lives
    in :mod:`tests.integration.test_paused_restart_e2e`; here we just
    confirm the store surface is wired correctly.
    """

    async def test_list_paused_returns_only_paused(self, real_store) -> None:
        await real_store.create_session(
            id="r", spec_json={}, trace_id=None, backend="inprocess"
        )
        await real_store.create_session(
            id="p", spec_json={}, trace_id=None, backend="inprocess"
        )
        await real_store.create_session(
            id="c", spec_json={}, trace_id=None, backend="inprocess"
        )
        await real_store.update_session_status("r", status="running")
        await real_store.update_session_status(
            "p", status="paused", paused_at=datetime.now(UTC)
        )
        await real_store.update_session_status("c", status="completed")

        rows = await real_store.list_paused()
        sids = {row["id"] for row in rows}
        assert sids == {"p"}

    async def test_list_paused_skips_null_paused_at(self, real_store) -> None:
        """Rows with status=paused but paused_at NULL are excluded."""
        await real_store.create_session(
            id="weird", spec_json={}, trace_id=None, backend="inprocess"
        )
        # Force status to paused without writing paused_at — emulates
        # a hypothetical inconsistency the recovery hook should
        # tolerate.
        await real_store.update_session_status("weird", status="paused")
        rows = await real_store.list_paused()
        assert rows == []

    async def test_recover_against_real_store_e2e(
        self, real_store
    ) -> None:
        """End-to-end smoke: create 2 paused rows, call recovery, assert
        fake manager observed both cases."""
        now = datetime.now(UTC)
        await real_store.create_session(
            id="under", spec_json={}, trace_id=None, backend="inprocess"
        )
        await real_store.create_session(
            id="over", spec_json={}, trace_id=None, backend="inprocess"
        )
        await real_store.update_session_status(
            "under",
            status="paused",
            paused_at=now - timedelta(minutes=1),
        )
        await real_store.update_session_status(
            "over",
            status="paused",
            paused_at=now - timedelta(minutes=60),
        )
        manager = _FakeManager()

        rearmed, cancelled = await recover_paused_timers(
            manager, real_store, paused_timeout_s=1800, now=now
        )

        assert (rearmed, cancelled) == (1, 1)
        rearmed_sids = {sid for sid, _ in manager.rearms}
        cancelled_sids = {sid for sid, _ in manager.cancels}
        assert rearmed_sids == {"under"}
        assert cancelled_sids == {"over"}

