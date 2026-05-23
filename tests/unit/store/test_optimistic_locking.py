"""Plan 7 D7.5 / Task 8 — optimistic-locking unit tests.

Five focused tests against :class:`SqlAlchemyStore` exercising the
optimistic-locking surface added in Task 8:

* version auto-increments when no ``expected_version`` is supplied
  (backwards compatibility with every pre-Task-8 call site);
* ``expected_version=0`` is treated as an *explicit* anchor (not the
  same as ``None``), so the very first transition out of a fresh row
  is testable;
* a second write that re-uses a stale ``expected_version`` raises
  :class:`ConcurrencyError`;
* the raised exception carries both the ``expected_version`` and the
  current ``actual_version`` so higher layers can include them in
  diagnostics / response bodies;
* the HITL upsert raises :class:`ConcurrencyError` on a stale
  ``expected_version`` without any retry — HITL is the explicitly
  "0-retry" path (see the resolve flow in
  :mod:`gg_relay.api.routers.hitl`).
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.store import (
    ConcurrencyError,
    SqlAlchemyStore,
    create_all_tables,
    make_async_engine,
)


@pytest_asyncio.fixture
async def engine(tmp_path) -> AsyncEngine:
    """Fresh on-disk SQLite per test (parity with test_store.py)."""
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/optimistic.db")
    await create_all_tables(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def store(engine: AsyncEngine) -> SqlAlchemyStore:
    return SqlAlchemyStore(engine)


# ── session-row optimistic locking ─────────────────────────────────


async def test_version_auto_increments_when_no_expected(
    store: SqlAlchemyStore,
) -> None:
    """No ``expected_version`` → read-then-bump (back-compat path)."""
    await store.create_session(
        id="s-auto", spec_json={}, trace_id=None, backend="inprocess"
    )
    assert await store.get_session_version("s-auto") == 0

    new_v = await store.update_session_status(
        "s-auto", status="running", started_at=datetime.now(UTC)
    )
    assert new_v == 1
    assert await store.get_session_version("s-auto") == 1

    # A second blind update bumps from 1 → 2.
    new_v = await store.update_session_status(
        "s-auto", status="completed"
    )
    assert new_v == 2
    assert await store.get_session_version("s-auto") == 2


async def test_expected_version_zero_explicit_branch(
    store: SqlAlchemyStore,
) -> None:
    """``expected_version=0`` is the *first* transition; must succeed once."""
    await store.create_session(
        id="s-zero", spec_json={}, trace_id=None, backend="inprocess"
    )
    # Brand-new row sits at version=0 via the schema default.
    assert await store.get_session_version("s-zero") == 0

    new_v = await store.update_session_status(
        "s-zero", status="running", expected_version=0
    )
    assert new_v == 1
    assert await store.get_session_version("s-zero") == 1


async def test_concurrent_update_raises_concurrency_error(
    store: SqlAlchemyStore,
) -> None:
    """Re-using a stale ``expected_version`` raises ConcurrencyError."""
    await store.create_session(
        id="s-race", spec_json={}, trace_id=None, backend="inprocess"
    )
    # First writer reads v=0 and bumps it to v=1.
    await store.update_session_status(
        "s-race", status="running", expected_version=0
    )
    # Second writer still believes v=0; the WHERE-version filter
    # matches nothing → ConcurrencyError.
    with pytest.raises(ConcurrencyError):
        await store.update_session_status(
            "s-race", status="paused", expected_version=0
        )
    # State unchanged by the failed second write — still RUNNING at v=1.
    row = await store.get_session("s-race")
    assert row is not None
    assert row["status"] == "running"
    assert row["version"] == 1


async def test_concurrency_error_carries_expected_actual(
    store: SqlAlchemyStore,
) -> None:
    """ConcurrencyError exposes expected_version + actual_version attrs."""
    await store.create_session(
        id="s-attrs", spec_json={}, trace_id=None, backend="inprocess"
    )
    # Move version to 3 so the assertion uses a non-trivial number.
    await store.update_session_status("s-attrs", status="running")  # 0→1
    await store.update_session_status("s-attrs", status="paused")  # 1→2
    await store.update_session_status("s-attrs", status="running")  # 2→3
    assert await store.get_session_version("s-attrs") == 3

    with pytest.raises(ConcurrencyError) as exc_info:
        await store.update_session_status(
            "s-attrs", status="completed", expected_version=1
        )
    assert exc_info.value.expected_version == 1
    assert exc_info.value.actual_version == 3


# ── HITL-row optimistic locking ────────────────────────────────────


async def test_hitl_version_check_no_retry(
    store: SqlAlchemyStore,
) -> None:
    """Stale HITL ``expected_version`` raises immediately (0 retry)."""
    await store.create_session(
        id="s-hitl", spec_json={}, trace_id=None, backend="inprocess"
    )
    await store.upsert_hitl(
        id="s-hitl:r1",
        session_id="s-hitl",
        tool="Bash",
        args_json={"cmd": "ls"},
        status="pending",
    )
    # Initial UPSERT inserts the row at version=0.
    assert await store.get_hitl_version("s-hitl:r1") == 0

    # First resolver wins: bumps version to 1.
    new_v = await store.upsert_hitl(
        id="s-hitl:r1",
        session_id="s-hitl",
        tool="Bash",
        args_json={"cmd": "ls"},
        status="accepted",
        resolved_at=datetime.now(UTC),
        resolver="admin-A",
        expected_version=0,
    )
    assert new_v == 1
    assert await store.get_hitl_version("s-hitl:r1") == 1

    # Second resolver re-uses the now-stale v=0 → ConcurrencyError,
    # NO retry (the resolve flow is intentionally 0-retry).
    with pytest.raises(ConcurrencyError) as exc_info:
        await store.upsert_hitl(
            id="s-hitl:r1",
            session_id="s-hitl",
            tool="Bash",
            args_json={"cmd": "ls"},
            status="denied",
            resolved_at=datetime.now(UTC),
            resolver="admin-B",
            expected_version=0,
        )
    assert exc_info.value.expected_version == 0
    assert exc_info.value.actual_version == 1
    # The winning decision is preserved — second writer's args were
    # silently rejected.
    row = await store.get_hitl("s-hitl:r1")
    assert row is not None
    assert row["status"] == "accepted"
    assert row["resolver"] == "admin-A"
