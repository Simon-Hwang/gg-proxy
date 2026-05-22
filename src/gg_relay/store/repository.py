"""SessionRepository — async DAO over SQLAlchemy Core.

The repository hides SQLAlchemy from the SessionManager and presents an
intent-oriented API (``create_session`` / ``append_frame`` / ``upsert_hitl``
etc). Every write commits in a single transaction; ``mark_in_flight_as_interrupted``
is the only multi-row update and runs under a single transaction.

All ``dict[str, Any]`` payloads MUST be pre-redacted by the caller — the
repository never inspects values for sensitive content.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import RowMapping, and_, delete, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.store.schema import frames, hitl_requests, sessions


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SessionRepository:
    """Async DAO over the three persistence tables.

    Construct once with the shared :class:`AsyncEngine` and reuse across
    handlers — methods open + close a per-call connection (SQLAlchemy
    handles pooling).
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    # ── sessions ───────────────────────────────────────────────────────

    async def create_session(
        self,
        *,
        id: str,
        spec_json: Mapping[str, Any],
        trace_id: str | None,
        backend: str,
        tags: Sequence[str] = (),
        submitted_at: datetime | None = None,
    ) -> None:
        """Insert a brand-new session in ``queued`` state.

        ``spec_json`` MUST already be redacted by the caller.
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(sessions).values(
                    id=id,
                    status="queued",
                    spec_json=dict(spec_json),
                    tags=list(tags),
                    submitted_at=submitted_at or _utcnow(),
                    trace_id=trace_id,
                    backend=backend,
                )
            )

    async def update_session_status(
        self,
        session_id: str,
        *,
        status: str | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        end_reason: str | None = None,
        runtime_id: str | None = None,
    ) -> None:
        """Patch the status / lifecycle columns for a session.

        Any ``None`` argument is left untouched (no overwrite of an existing
        value). Caller is responsible for keeping status transitions sane.
        """
        values: dict[str, Any] = {}
        if status is not None:
            values["status"] = status
        if started_at is not None:
            values["started_at"] = started_at
        if ended_at is not None:
            values["ended_at"] = ended_at
        if end_reason is not None:
            values["end_reason"] = end_reason
        if runtime_id is not None:
            values["runtime_id"] = runtime_id
        if not values:
            return
        async with self._engine.begin() as conn:
            await conn.execute(
                update(sessions).where(sessions.c.id == session_id).values(**values)
            )

    async def list_sessions(
        self,
        *,
        status: str | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RowMapping]:
        """List sessions newest-first, optionally filtered by status."""
        stmt = select(sessions).order_by(sessions.c.submitted_at.desc())
        if status is not None:
            stmt = stmt.where(sessions.c.status == status)
        stmt = stmt.limit(limit).offset(offset)
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            rows = result.mappings().all()
        if tag is None:
            return list(rows)
        return [r for r in rows if tag in (r["tags"] or [])]

    async def get_session(self, session_id: str) -> RowMapping | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(sessions).where(sessions.c.id == session_id)
            )
            row = result.mappings().first()
        return row

    async def delete_session(self, session_id: str) -> None:
        """Delete a session row. ``frames`` + ``hitl_requests`` cascade."""
        async with self._engine.begin() as conn:
            await conn.execute(delete(sessions).where(sessions.c.id == session_id))

    async def mark_in_flight_as_interrupted(self) -> list[str]:
        """Move every row whose ``status='running'`` to ``interrupted``.

        Returns the list of session ids that were touched. Idempotent;
        re-running returns ``[]`` because no rows match the predicate anymore.
        """
        now = _utcnow()
        async with self._engine.begin() as conn:
            # Collect ids first so we can return them; then update.
            ids = [
                r[0]
                for r in (
                    await conn.execute(
                        select(sessions.c.id).where(sessions.c.status == "running")
                    )
                ).fetchall()
            ]
            if ids:
                await conn.execute(
                    update(sessions)
                    .where(sessions.c.status == "running")
                    .values(
                        status="interrupted",
                        ended_at=now,
                        end_reason="interrupted_on_startup",
                    )
                )
        return ids

    # ── frames ─────────────────────────────────────────────────────────

    async def append_frame(
        self,
        session_id: str,
        *,
        seq: int,
        ts: datetime,
        type_: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Append a single frame. Caller MUST supply a redacted payload."""
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(frames).values(
                    session_id=session_id,
                    seq=seq,
                    ts=ts,
                    type=type_,
                    payload=dict(payload),
                )
            )

    async def list_frames(
        self,
        session_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RowMapping]:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(frames)
                .where(frames.c.session_id == session_id)
                .order_by(frames.c.seq.asc())
                .limit(limit)
                .offset(offset)
            )
            return list(result.mappings().all())

    async def prune_frames_older_than(self, *, cutoff: datetime) -> int:
        """Delete frames whose ``ts`` is strictly less than ``cutoff``.

        Returns the number of rows deleted.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(delete(frames).where(frames.c.ts < cutoff))
        return int(result.rowcount or 0)

    # ── hitl ───────────────────────────────────────────────────────────

    async def upsert_hitl(
        self,
        *,
        id: str,
        session_id: str,
        tool: str,
        args_json: Mapping[str, Any],
        status: str,
        created_at: datetime | None = None,
        resolved_at: datetime | None = None,
        reason: str | None = None,
        resolver: str | None = None,
    ) -> None:
        """Insert-or-update a HITL request row.

        Uses SQLite's ``ON CONFLICT DO UPDATE`` when the dialect supports it
        and falls back to a SELECT-then-UPDATE/INSERT for portability
        (Postgres prod path will also support the SQLite UPSERT shape via
        SQLAlchemy 2.0's dialect-specific helpers).
        """
        values: dict[str, Any] = {
            "id": id,
            "session_id": session_id,
            "tool": tool,
            "args_json": dict(args_json),
            "status": status,
            "created_at": created_at or _utcnow(),
            "resolved_at": resolved_at,
            "reason": reason,
            "resolver": resolver,
        }
        dialect = self._engine.dialect.name
        async with self._engine.begin() as conn:
            if dialect == "sqlite":
                stmt = sqlite_insert(hitl_requests).values(**values)
                upd = {
                    k: v
                    for k, v in values.items()
                    if k not in {"id", "session_id", "created_at"}
                }
                stmt = stmt.on_conflict_do_update(
                    index_elements=[hitl_requests.c.id], set_=upd
                )
                await conn.execute(stmt)
                return
            try:
                await conn.execute(insert(hitl_requests).values(**values))
            except IntegrityError:
                upd = {
                    k: v
                    for k, v in values.items()
                    if k not in {"id", "session_id", "created_at"}
                }
                await conn.execute(
                    update(hitl_requests)
                    .where(hitl_requests.c.id == id)
                    .values(**upd)
                )

    async def get_hitl(self, req_id: str) -> RowMapping | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(hitl_requests).where(hitl_requests.c.id == req_id)
            )
            return result.mappings().first()

    async def list_pending_hitl(
        self, *, session_id: str | None = None
    ) -> list[RowMapping]:
        clauses = [hitl_requests.c.status == "pending"]
        if session_id is not None:
            clauses.append(hitl_requests.c.session_id == session_id)
        stmt = (
            select(hitl_requests)
            .where(and_(*clauses))
            .order_by(hitl_requests.c.created_at.asc())
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            return list(result.mappings().all())
