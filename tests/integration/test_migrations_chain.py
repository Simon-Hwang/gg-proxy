"""Migration chain integrity — Plan 7 Tasks 6 + 7 (D7.5 / D7.17).

Verifies the full ``0001 → 0002 → 0003 → 0004`` upgrade path plus the
per-revision downgrade roundtrips on SQLite, and (optionally) the same
chain on a real Postgres dialect via ``RELAY_TEST_POSTGRES_URL`` when a
Docker daemon is reachable. The test reuses the subprocess alembic
helper from :mod:`test_session_aggregates_migration` rather than
re-implementing it; the indirection avoids re-entering the running
pytest-asyncio loop from inside ``alembic upgrade`` (which itself calls
``asyncio.run`` in ``store/migrations/env.py``).

Task 7 (events table, 0004) appends three checks below:
  * ``test_chain_0001_to_0004_upgrade``       — events table + indexes
  * ``test_downgrade_0004_to_0003_roundtrip`` — events gone, 0003 columns kept
  * ``test_chain_0001_to_0004_postgres``      — same on Postgres dialect
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text

from gg_relay.store import make_async_engine

from .test_session_aggregates_migration import (
    _run_alembic,
    _run_downgrade,
    _run_upgrade,
)

pytestmark = pytest.mark.asyncio


# ── SQLite chain (default; runs in every CI job) ────────────────────


@pytest_asyncio.fixture
async def sqlite_db_url(tmp_path: Path):
    """Yield an empty SQLite file URL; caller drives alembic."""
    db_file = tmp_path / "chain.db"
    yield f"sqlite+aiosqlite:///{db_file}"


async def _columns(async_url: str, table: str) -> set[str]:
    engine = make_async_engine(async_url)
    try:
        async with engine.connect() as conn:

            def _inspect(sync_conn):
                return {c["name"] for c in inspect(sync_conn).get_columns(table)}

            return await conn.run_sync(_inspect)
    finally:
        await engine.dispose()


async def _table_names(async_url: str) -> set[str]:
    engine = make_async_engine(async_url)
    try:
        async with engine.connect() as conn:

            def _inspect(sync_conn):
                return set(inspect(sync_conn).get_table_names())

            return await conn.run_sync(_inspect)
    finally:
        await engine.dispose()


async def _index_names(async_url: str, table: str) -> set[str]:
    engine = make_async_engine(async_url)
    try:
        async with engine.connect() as conn:

            def _inspect(sync_conn):
                return {i["name"] for i in inspect(sync_conn).get_indexes(table)}

            return await conn.run_sync(_inspect)
    finally:
        await engine.dispose()


async def test_chain_0001_to_0003_upgrade(sqlite_db_url: str):
    """0001 → 0002 → 0003 顺次 upgrade，最终 sessions/hitl_requests 含新列."""
    _run_upgrade(sqlite_db_url, "head")
    sess_cols = await _columns(sqlite_db_url, "sessions")
    hitl_cols = await _columns(sqlite_db_url, "hitl_requests")
    assert "version" in sess_cols, f"sessions.version missing: {sess_cols}"
    assert "paused_at" in sess_cols, f"sessions.paused_at missing: {sess_cols}"
    assert "version" in hitl_cols, f"hitl_requests.version missing: {hitl_cols}"
    # Sanity: 0002 columns still present (i.e. 0003 didn't accidentally
    # rebuild the table without them).
    for col in {"input_tokens", "output_tokens", "cost_usd", "turn_count"}:
        assert col in sess_cols, f"0002 column {col!r} lost after 0003"


async def test_downgrade_0003_to_0002_roundtrip(sqlite_db_url: str):
    """upgrade head → downgrade -1 → 新列消失，0002 列保留."""
    _run_upgrade(sqlite_db_url, "head")
    _run_downgrade(sqlite_db_url, "0002")
    sess_cols = await _columns(sqlite_db_url, "sessions")
    hitl_cols = await _columns(sqlite_db_url, "hitl_requests")
    for col in {"version", "paused_at"}:
        assert col not in sess_cols, f"sessions.{col} survived downgrade"
    assert "version" not in hitl_cols, "hitl_requests.version survived downgrade"
    # 0002 columns still alive — only 0003 was rolled back.
    assert "input_tokens" in sess_cols
    assert "turn_count" in sess_cols


async def test_existing_rows_get_default_zero(sqlite_db_url: str):
    """0002 状态插一行 session/hitl_request → upgrade 0003 → 已有行 version=0."""
    _run_upgrade(sqlite_db_url, "0002")
    engine = make_async_engine(sqlite_db_url)
    try:
        async with engine.begin() as conn:
            now = datetime.now(UTC).isoformat()
            await conn.execute(
                text(
                    "INSERT INTO sessions "
                    "(id, status, spec_json, tags, submitted_at, backend, "
                    " input_tokens, output_tokens, cost_usd, turn_count) "
                    "VALUES (:id, 'queued', '{}', '[]', :ts, 'inprocess', "
                    " 0, 0, 0, 0)"
                ),
                {"id": "sid-pre", "ts": now},
            )
            await conn.execute(
                text(
                    "INSERT INTO hitl_requests "
                    "(id, session_id, tool, args_json, status, created_at) "
                    "VALUES (:id, :sid, 'Bash', '{}', 'pending', :ts)"
                ),
                {"id": "h-pre", "sid": "sid-pre", "ts": now},
            )
    finally:
        await engine.dispose()
    # Now run 0003 — existing rows must get version=0 via server_default.
    _run_upgrade(sqlite_db_url, "head")
    engine = make_async_engine(sqlite_db_url)
    try:
        async with engine.connect() as conn:
            sess_row = (
                await conn.execute(
                    text(
                        "SELECT version, paused_at FROM sessions "
                        "WHERE id='sid-pre'"
                    )
                )
            ).first()
            hitl_row = (
                await conn.execute(
                    text("SELECT version FROM hitl_requests WHERE id='h-pre'")
                )
            ).first()
        assert sess_row is not None
        assert sess_row[0] == 0, f"sessions.version default not applied: {sess_row[0]!r}"
        assert sess_row[1] is None, f"sessions.paused_at should be NULL: {sess_row[1]!r}"
        assert hitl_row is not None
        assert hitl_row[0] == 0, (
            f"hitl_requests.version default not applied: {hitl_row[0]!r}"
        )
    finally:
        await engine.dispose()


# ── Postgres chain (opt-in via RELAY_TEST_POSTGRES_URL) ─────────────


def _postgres_async_url() -> str | None:
    """Return ``RELAY_TEST_POSTGRES_URL`` normalized to async driver, or None.

    The CI ``requires_docker`` job installs the ``postgres`` extra and
    boots a postgres container; setting ``RELAY_TEST_POSTGRES_URL=
    postgresql://gg:gg@127.0.0.1:5432/gg`` (or the asyncpg variant)
    activates this test. Locally without docker, the env var is unset
    and the test is skipped.
    """
    raw = os.environ.get("RELAY_TEST_POSTGRES_URL")
    if not raw:
        return None
    if raw.startswith("postgresql+asyncpg://"):
        return raw
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw[len("postgresql://") :]
    return raw


@pytest.mark.requires_docker
async def test_chain_on_postgres():
    """Postgres dialect 跑完整 0001→0002→0003 + downgrade roundtrip.

    Runs only when ``RELAY_TEST_POSTGRES_URL`` is set — the CI
    ``requires_docker`` job exports it before invoking pytest. The
    test cleans up by downgrading back to ``base`` so the database
    can be reused across runs.
    """
    url = _postgres_async_url()
    if url is None:
        pytest.skip("RELAY_TEST_POSTGRES_URL not set; skipping Postgres chain test")
    # Start from a clean slate so a previously-failed run doesn't
    # leave tables behind.
    _run_alembic(url, "downgrade", "base")
    try:
        _run_upgrade(url, "head")
        sess_cols = await _columns(url, "sessions")
        hitl_cols = await _columns(url, "hitl_requests")
        assert "version" in sess_cols
        assert "paused_at" in sess_cols
        assert "version" in hitl_cols
        # Downgrade roundtrip — 0003 columns gone, 0002 columns survive.
        _run_downgrade(url, "0002")
        sess_cols = await _columns(url, "sessions")
        hitl_cols = await _columns(url, "hitl_requests")
        assert "version" not in sess_cols
        assert "paused_at" not in sess_cols
        assert "version" not in hitl_cols
        assert "input_tokens" in sess_cols
    finally:
        _run_alembic(url, "downgrade", "base")


# ── Plan 7 Task 7 / D7.17: events table (0004) ──────────────────────


async def test_chain_0001_to_0004_upgrade(sqlite_db_url: str):
    """0001 → 0002 → 0003 → 0004 顺次 upgrade，events 表 + indexes 就位."""
    _run_upgrade(sqlite_db_url, "head")

    tables = await _table_names(sqlite_db_url)
    assert "events" in tables, f"events table missing after 0004: {tables}"

    cols = await _columns(sqlite_db_url, "events")
    expected = {
        "event_id",
        "ts",
        "type",
        "session_id",
        "payload",
        "delivery_tier",
    }
    assert expected <= cols, f"events columns missing: expected {expected}, got {cols}"

    indexes = await _index_names(sqlite_db_url, "events")
    assert "ix_events_ts" in indexes, f"ix_events_ts missing: {indexes}"
    assert "ix_events_session_id" in indexes, f"ix_events_session_id missing: {indexes}"

    # Sanity — 0003 columns survived the new migration.
    sess_cols = await _columns(sqlite_db_url, "sessions")
    assert "version" in sess_cols
    assert "paused_at" in sess_cols


async def test_downgrade_0004_to_0003_roundtrip(sqlite_db_url: str):
    """upgrade head → downgrade -1 → events 表消失但 0003 列保留."""
    _run_upgrade(sqlite_db_url, "head")
    _run_downgrade(sqlite_db_url, "0003")

    tables = await _table_names(sqlite_db_url)
    assert "events" not in tables, f"events survived downgrade: {tables}"

    # 0003 columns are untouched — only 0004 was rolled back.
    sess_cols = await _columns(sqlite_db_url, "sessions")
    hitl_cols = await _columns(sqlite_db_url, "hitl_requests")
    assert "version" in sess_cols, "sessions.version disappeared on 0004 downgrade"
    assert "paused_at" in sess_cols, "sessions.paused_at disappeared on 0004 downgrade"
    assert "version" in hitl_cols, "hitl_requests.version disappeared on 0004 downgrade"


@pytest.mark.requires_docker
async def test_chain_0001_to_0004_postgres():
    """Postgres dialect 跑完整 0001→0002→0003→0004 + downgrade roundtrip.

    Runs only when ``RELAY_TEST_POSTGRES_URL`` is set. Cleans up via
    ``downgrade base`` so the database can be reused across runs.
    """
    url = _postgres_async_url()
    if url is None:
        pytest.skip("RELAY_TEST_POSTGRES_URL not set; skipping Postgres chain test")
    _run_alembic(url, "downgrade", "base")
    try:
        _run_upgrade(url, "head")
        tables = await _table_names(url)
        assert "events" in tables
        cols = await _columns(url, "events")
        assert {
            "event_id",
            "ts",
            "type",
            "session_id",
            "payload",
            "delivery_tier",
        } <= cols
        indexes = await _index_names(url, "events")
        assert "ix_events_ts" in indexes
        assert "ix_events_session_id" in indexes
        # Downgrade roundtrip — drop 0005 + 0004; verify only 0003
        # columns survive at the 0003 anchor.
        _run_downgrade(url, "0003")
        tables = await _table_names(url)
        assert "events" not in tables
        sess_cols = await _columns(url, "sessions")
        assert "version" in sess_cols
        assert "paused_at" in sess_cols
        assert "owner" not in sess_cols
        assert "description" not in sess_cols
    finally:
        _run_alembic(url, "downgrade", "base")


# ── Plan 7 Task 6b / D7.26: collaboration metadata (0005) ───────────


async def test_chain_0001_to_0005_upgrade(sqlite_db_url: str):
    """0001 → … → 0005 顺次 upgrade，sessions 含 owner / description /
    ix_sessions_owner index。事件表 (0004) 一并保留。"""
    _run_upgrade(sqlite_db_url, "head")

    sess_cols = await _columns(sqlite_db_url, "sessions")
    assert "owner" in sess_cols, f"sessions.owner missing: {sess_cols}"
    assert "description" in sess_cols, (
        f"sessions.description missing: {sess_cols}"
    )

    indexes = await _index_names(sqlite_db_url, "sessions")
    assert "ix_sessions_owner" in indexes, (
        f"ix_sessions_owner missing: {indexes}"
    )

    # Sanity — 0004 events table is still present (we didn't accidentally
    # rebuild sessions in a way that wiped the cross-table state).
    tables = await _table_names(sqlite_db_url)
    assert "events" in tables, (
        f"events table missing after 0005 upgrade: {tables}"
    )


async def test_downgrade_0005_to_0004_roundtrip(sqlite_db_url: str):
    """upgrade head → downgrade -1 → owner/description 消失但 events 表
    + 0003 columns 仍保留 (验证只回滚 0005)."""
    _run_upgrade(sqlite_db_url, "head")
    _run_downgrade(sqlite_db_url, "0004")

    sess_cols = await _columns(sqlite_db_url, "sessions")
    assert "owner" not in sess_cols, (
        "sessions.owner survived downgrade to 0004"
    )
    assert "description" not in sess_cols, (
        "sessions.description survived downgrade to 0004"
    )

    indexes = await _index_names(sqlite_db_url, "sessions")
    assert "ix_sessions_owner" not in indexes, (
        "ix_sessions_owner survived downgrade to 0004"
    )

    # 0004 + 0003 columns must still be in place — only 0005 was rolled
    # back.
    tables = await _table_names(sqlite_db_url)
    assert "events" in tables, "events table lost on 0005 downgrade"
    assert "version" in sess_cols, "0003 version column lost on 0005 downgrade"
    assert "paused_at" in sess_cols, (
        "0003 paused_at column lost on 0005 downgrade"
    )


# ── Plan 8 Task 5 / D8.4: audit_log table (0006) ────────────────────


async def test_chain_0001_to_0006_upgrade(sqlite_db_url: str):
    """0001 → … → 0006 顺次 upgrade，audit_log 表 + 3 个 index 就位.

    Plan 8 D8.4 / Task 5. Verifies ``audit_log`` was created with the
    full schema (id PK / ts / actor / action / target_type+id /
    metadata_json / request_id) and the three composite indexes
    (``ix_audit_log_ts`` / ``ix_audit_log_actor_ts`` /
    ``ix_audit_log_target``) exist. Sanity: 0005 collaboration columns
    survived (we didn't accidentally rebuild ``sessions``).
    """
    _run_upgrade(sqlite_db_url, "head")

    tables = await _table_names(sqlite_db_url)
    assert "audit_log" in tables, (
        f"audit_log table missing after 0006: {tables}"
    )

    cols = await _columns(sqlite_db_url, "audit_log")
    expected = {
        "id",
        "ts",
        "actor",
        "action",
        "target_type",
        "target_id",
        "metadata_json",
        "request_id",
    }
    assert expected <= cols, (
        f"audit_log columns missing: expected {expected}, got {cols}"
    )

    indexes = await _index_names(sqlite_db_url, "audit_log")
    assert "ix_audit_log_ts" in indexes, (
        f"ix_audit_log_ts missing: {indexes}"
    )
    assert "ix_audit_log_actor_ts" in indexes, (
        f"ix_audit_log_actor_ts missing: {indexes}"
    )
    assert "ix_audit_log_target" in indexes, (
        f"ix_audit_log_target missing: {indexes}"
    )

    # Sanity — 0005 owner/description columns still present.
    sess_cols = await _columns(sqlite_db_url, "sessions")
    assert "owner" in sess_cols, "0005 owner lost after 0006 upgrade"
    assert "description" in sess_cols, (
        "0005 description lost after 0006 upgrade"
    )


async def test_downgrade_0006_to_0005_roundtrip(sqlite_db_url: str):
    """upgrade head → downgrade -1 → audit_log 表 + indexes 消失,
    0005 collaboration columns 仍保留 (验证只回滚 0006)."""
    _run_upgrade(sqlite_db_url, "head")
    _run_downgrade(sqlite_db_url, "0005")

    tables = await _table_names(sqlite_db_url)
    assert "audit_log" not in tables, (
        f"audit_log survived downgrade to 0005: {tables}"
    )

    # 0005 columns + 0004 events table must still be alive — only 0006
    # was rolled back.
    sess_cols = await _columns(sqlite_db_url, "sessions")
    assert "owner" in sess_cols, "0005 owner lost on 0006 downgrade"
    assert "description" in sess_cols, (
        "0005 description lost on 0006 downgrade"
    )
    assert "events" in tables, "0004 events table lost on 0006 downgrade"


# ── Plan 8 Task 7 / D8.5: session_comments table (0007) ──────────────


async def test_chain_0001_to_0007_upgrade(sqlite_db_url: str):
    """0001 → … → 0007 顺次 upgrade，session_comments 表 + 复合 index 就位.

    Plan 8 D8.5 / Task 7. Verifies ``session_comments`` was created
    with the full schema (id PK / session_id FK CASCADE / author /
    body_markdown / body_html / created_at / updated_at /
    deleted_at) and the composite ``ix_session_comments_session_created``
    index exists. Sanity: 0006 ``audit_log`` table is still alive
    (we didn't accidentally rebuild it).
    """
    _run_upgrade(sqlite_db_url, "head")

    tables = await _table_names(sqlite_db_url)
    assert "session_comments" in tables, (
        f"session_comments table missing after 0007: {tables}"
    )

    cols = await _columns(sqlite_db_url, "session_comments")
    expected = {
        "id",
        "session_id",
        "author",
        "body_markdown",
        "body_html",
        "created_at",
        "updated_at",
        "deleted_at",
    }
    assert expected <= cols, (
        f"session_comments columns missing: expected {expected}, got {cols}"
    )

    indexes = await _index_names(sqlite_db_url, "session_comments")
    assert "ix_session_comments_session_created" in indexes, (
        f"ix_session_comments_session_created missing: {indexes}"
    )

    # Sanity — 0006 audit_log table still present.
    assert "audit_log" in tables, "0006 audit_log lost after 0007 upgrade"


async def test_downgrade_0007_to_0006_roundtrip(sqlite_db_url: str):
    """upgrade head → downgrade -1 → session_comments 表 + index 消失,
    0006 audit_log 表仍保留 (验证只回滚 0007)."""
    _run_upgrade(sqlite_db_url, "head")
    _run_downgrade(sqlite_db_url, "0006")

    tables = await _table_names(sqlite_db_url)
    assert "session_comments" not in tables, (
        f"session_comments survived downgrade to 0006: {tables}"
    )

    # 0006 audit_log + earlier migrations must still be alive — only
    # 0007 was rolled back.
    assert "audit_log" in tables, (
        "0006 audit_log table lost on 0007 downgrade"
    )
    sess_cols = await _columns(sqlite_db_url, "sessions")
    assert "owner" in sess_cols, "0005 owner lost on 0007 downgrade"


# ── Plan 8 Task 9 / D8.6: parent_session_id (0008) ───────────────────


async def test_chain_0001_to_0008_upgrade(sqlite_db_url: str):
    """0001 → … → 0008 顺次 upgrade，sessions 含 parent_session_id 列
    + ix_sessions_parent_session_id index.

    Plan 8 D8.6 / Task 9. Verifies the retry-lineage column landed on
    ``sessions`` and the equality index that powers
    :meth:`SqlAlchemyStore.list_children_of_session` is present.
    Sanity: 0007 ``session_comments`` table is still alive (we
    didn't accidentally rebuild it).
    """
    _run_upgrade(sqlite_db_url, "head")

    sess_cols = await _columns(sqlite_db_url, "sessions")
    assert "parent_session_id" in sess_cols, (
        f"sessions.parent_session_id missing after 0008: {sess_cols}"
    )

    indexes = await _index_names(sqlite_db_url, "sessions")
    assert "ix_sessions_parent_session_id" in indexes, (
        f"ix_sessions_parent_session_id missing: {indexes}"
    )

    # Sanity — 0007 session_comments table still present.
    tables = await _table_names(sqlite_db_url)
    assert "session_comments" in tables, (
        "0007 session_comments lost after 0008 upgrade"
    )


async def test_downgrade_0008_to_0007_roundtrip(sqlite_db_url: str):
    """upgrade head → downgrade -1 → parent_session_id + index 消失，
    0007 session_comments 表仍保留 (验证只回滚 0008)."""
    _run_upgrade(sqlite_db_url, "head")
    _run_downgrade(sqlite_db_url, "0007")

    sess_cols = await _columns(sqlite_db_url, "sessions")
    assert "parent_session_id" not in sess_cols, (
        "sessions.parent_session_id survived downgrade to 0007"
    )

    indexes = await _index_names(sqlite_db_url, "sessions")
    assert "ix_sessions_parent_session_id" not in indexes, (
        "ix_sessions_parent_session_id survived downgrade to 0007"
    )

    # 0007 session_comments + earlier migrations must still be alive
    # — only 0008 was rolled back.
    tables = await _table_names(sqlite_db_url)
    assert "session_comments" in tables, (
        "0007 session_comments lost on 0008 downgrade"
    )
    assert "audit_log" in tables, (
        "0006 audit_log lost on 0008 downgrade"
    )


# ── Plan 8 Task 13 / D8.21: session_favorites table (0009) ──────────


async def test_chain_0001_to_0009_upgrade(sqlite_db_url: str):
    """0001 → … → 0009 顺次 upgrade，session_favorites 表 + unique constraint
    + composite index 就位.

    Plan 8 D8.21 / Task 13. Verifies the per-user favorites table
    landed with the full schema (id PK / session_id FK CASCADE /
    user_label / created_at) and that both the unique constraint
    ``uq_session_favorites_session_user`` and the composite
    ``ix_session_favorites_user_created`` index are present.
    Sanity: 0008 ``sessions.parent_session_id`` column is still
    alive (we didn't accidentally rebuild ``sessions``).
    """
    _run_upgrade(sqlite_db_url, "head")

    tables = await _table_names(sqlite_db_url)
    assert "session_favorites" in tables, (
        f"session_favorites table missing after 0009: {tables}"
    )

    cols = await _columns(sqlite_db_url, "session_favorites")
    expected = {"id", "session_id", "user_label", "created_at"}
    assert expected <= cols, (
        f"session_favorites columns missing: expected {expected}, got {cols}"
    )

    indexes = await _index_names(sqlite_db_url, "session_favorites")
    assert "ix_session_favorites_user_created" in indexes, (
        f"ix_session_favorites_user_created missing: {indexes}"
    )

    # Unique constraint: SQLite surfaces it as a unique index named
    # ``uq_session_favorites_session_user``. Inspect via
    # :func:`sqlalchemy.inspect`'s ``get_unique_constraints``.
    engine = make_async_engine(sqlite_db_url)
    try:
        async with engine.connect() as conn:

            def _inspect(sync_conn):
                return {
                    uc["name"]
                    for uc in inspect(sync_conn).get_unique_constraints(
                        "session_favorites"
                    )
                }

            uniques = await conn.run_sync(_inspect)
    finally:
        await engine.dispose()
    assert "uq_session_favorites_session_user" in uniques, (
        f"uq_session_favorites_session_user missing: {uniques}"
    )

    # Sanity — 0008 parent_session_id column still present.
    sess_cols = await _columns(sqlite_db_url, "sessions")
    assert "parent_session_id" in sess_cols, (
        "0008 parent_session_id lost after 0009 upgrade"
    )


async def test_downgrade_0009_to_0008_roundtrip(sqlite_db_url: str):
    """upgrade head → downgrade -1 → session_favorites 表 + index 消失,
    0008 parent_session_id 列仍保留 (验证只回滚 0009)."""
    _run_upgrade(sqlite_db_url, "head")
    _run_downgrade(sqlite_db_url, "0008")

    tables = await _table_names(sqlite_db_url)
    assert "session_favorites" not in tables, (
        f"session_favorites survived downgrade to 0008: {tables}"
    )

    # 0008 parent_session_id + earlier migrations must still be alive
    # — only 0009 was rolled back.
    sess_cols = await _columns(sqlite_db_url, "sessions")
    assert "parent_session_id" in sess_cols, (
        "0008 parent_session_id lost on 0009 downgrade"
    )
    assert "session_comments" in tables, (
        "0007 session_comments lost on 0009 downgrade"
    )
