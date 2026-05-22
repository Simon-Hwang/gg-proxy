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

import sqlalchemy as sa
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

    async def update_session_aggregates(
        self,
        session_id: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        turn_count: int = 0,
    ) -> None:
        """Plan 6 D6.12 — write the four per-session aggregates.

        Called from :meth:`SessionManager._record_session_end` once a
        session reaches a terminal state. All four values are kept
        non-null at the schema level (default 0) so the dashboard's
        chart query can sum / group without coalescing.

        Idempotent — calling twice for the same id overwrites with
        whatever the caller passes, so partial retries are safe.
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                update(sessions)
                .where(sessions.c.id == session_id)
                .values(
                    input_tokens=int(input_tokens),
                    output_tokens=int(output_tokens),
                    cost_usd=float(cost_usd),
                    turn_count=int(turn_count),
                )
            )

    async def aggregate_tokens_by_bucket(
        self,
        *,
        window_s: int,
        bucket_s: int,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Plan 6 D6.12 — bucketed token / cost time-series for the
        dashboard's global chart.

        Returns a list of dicts shaped like::

            [
                {"bucket_start": dt, "input_tokens": 12000,
                 "output_tokens": 8000, "cost_usd": 0.42, "sessions": 7},
                ...
            ]

        sorted by ``bucket_start`` ascending. Sessions whose
        ``ended_at`` is NULL or older than ``now - window_s`` are
        excluded. The query path branches on dialect because SQLite and
        Postgres compute time buckets very differently:
          * **SQLite** — converts ``ended_at`` to a unix epoch via
            ``strftime('%s')``, integer-divides by ``bucket_s``, then
            multiplies back. Coarse but dependency-free.
          * **Postgres** — uses ``date_bin(interval, ts, anchor)``
            which is the canonical bucketing primitive (PG 14+).

        Other dialects raise ``NotImplementedError`` — gg-relay only
        supports SQLite (dev/test) + Postgres (prod) per Plan 4 §8.
        """
        anchor = now or _utcnow()
        cutoff = anchor.timestamp() - window_s
        dialect = self._engine.dialect.name
        async with self._engine.connect() as conn:
            if dialect == "sqlite":
                # strftime('%s', ts) returns the unix epoch as a text
                # string; CAST to integer so the arithmetic is exact.
                # Bucket label = floor(epoch / bucket_s) * bucket_s.
                stmt = sa.text(
                    """
                    SELECT
                        (CAST(strftime('%s', ended_at) AS INTEGER) / :bucket_s)
                            * :bucket_s AS bucket_epoch,
                        SUM(input_tokens) AS input_tokens,
                        SUM(output_tokens) AS output_tokens,
                        SUM(cost_usd) AS cost_usd,
                        COUNT(id) AS sessions
                    FROM sessions
                    WHERE ended_at IS NOT NULL
                      AND CAST(strftime('%s', ended_at) AS INTEGER) >= :cutoff
                    GROUP BY bucket_epoch
                    ORDER BY bucket_epoch ASC
                    """
                )
                params = {"bucket_s": bucket_s, "cutoff": int(cutoff)}
                rows = (await conn.execute(stmt, params)).mappings().all()
                out: list[dict[str, Any]] = []
                for r in rows:
                    bucket_start = datetime.fromtimestamp(
                        int(r["bucket_epoch"]), tz=UTC
                    )
                    out.append(
                        {
                            "bucket_start": bucket_start,
                            "input_tokens": int(r["input_tokens"] or 0),
                            "output_tokens": int(r["output_tokens"] or 0),
                            "cost_usd": float(r["cost_usd"] or 0.0),
                            "sessions": int(r["sessions"] or 0),
                        }
                    )
                return out
            if dialect in {"postgresql", "postgres"}:
                stmt = sa.text(
                    """
                    SELECT
                        date_bin(
                            (:bucket_s || ' seconds')::interval,
                            ended_at,
                            timestamptz 'epoch'
                        ) AS bucket_start,
                        SUM(input_tokens) AS input_tokens,
                        SUM(output_tokens) AS output_tokens,
                        SUM(cost_usd) AS cost_usd,
                        COUNT(id) AS sessions
                    FROM sessions
                    WHERE ended_at IS NOT NULL
                      AND ended_at >= NOW() - (:window_s || ' seconds')::interval
                    GROUP BY bucket_start
                    ORDER BY bucket_start ASC
                    """
                )
                params = {"bucket_s": bucket_s, "window_s": window_s}
                rows = (await conn.execute(stmt, params)).mappings().all()
                return [
                    {
                        "bucket_start": r["bucket_start"],
                        "input_tokens": int(r["input_tokens"] or 0),
                        "output_tokens": int(r["output_tokens"] or 0),
                        "cost_usd": float(r["cost_usd"] or 0.0),
                        "sessions": int(r["sessions"] or 0),
                    }
                    for r in rows
                ]
            raise NotImplementedError(
                f"aggregate_tokens_by_bucket: unsupported dialect {dialect!r}"
            )

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
