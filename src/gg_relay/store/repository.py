"""SqlAlchemyStore — async DAO over SQLAlchemy Core.

The store hides SQLAlchemy from the SessionManager and presents an
intent-oriented API (``create_session`` / ``append_frame`` / ``upsert_hitl``
etc). Every write commits in a single transaction; ``mark_in_flight_as_interrupted``
is the only multi-row update and runs under a single transaction.

All ``dict[str, Any]`` payloads MUST be pre-redacted by the caller — the
store never inspects values for sensitive content.

History: renamed from ``SessionRepository`` in Plan 7 Task 5 (D7.4) so
the concrete name no longer conflicts with the
:class:`gg_relay.store.protocol.SessionStore` Protocol. A
:class:`SessionRepository` subclass alias is kept as a deprecated
shim until 0.8.0.
"""
from __future__ import annotations

import base64
import hashlib
import json
import warnings
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import RowMapping, and_, delete, insert, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.store.exceptions import (
    ConcurrencyError,
    CursorFilterMismatchError,
    CursorInvalidError,
)
from gg_relay.store.schema import (
    audit_log,
    frames,
    hitl_requests,
    session_comments,
    sessions,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ── Cursor pagination helpers (Plan 7 D7.6 / Task 9) ─────────────────
# Cursors are opaque urlsafe-base64-encoded JSON payloads carrying the
# ``submitted_at`` + ``id`` of the last row in the previous page plus
# a short hash of the filter combination they were issued under. The
# hash lets the server reject a cursor that was minted against a
# different ``status`` / ``tag`` combo rather than silently returning
# misleading rows. Cursors are NOT signed — Plan 11 will add HMAC
# tamper detection; for now we treat them as advisory pagination
# tokens (a tampered cursor at worst causes a 400 or skips/repeats a
# row from the requester's own view, not a privilege escalation).


def _filter_hash(*, status: str | None, tag: str | None) -> str:
    """Compact deterministic identifier for the active filter combo.

    SHA1 + ``[:12]`` keeps cursors short while remaining collision-
    resistant for the tiny domain we care about (a handful of
    status/tag values per deployment). The same inputs always
    produce the same hash so a client paging without changing
    filters keeps flowing through cursors deterministically.
    """
    return hashlib.sha1(
        f"{status or '*'}|{tag or '*'}".encode()
    ).hexdigest()[:12]


def _encode_cursor(
    *, submitted_at: datetime, id: str, filter_hash: str
) -> str:
    """Pack ``submitted_at`` + ``id`` + ``filter_hash`` into an opaque token.

    The payload is JSON for forward-compat (Plan 11 may add an HMAC
    field) and the wire encoding is urlsafe-base64 without padding so
    the cursor can ride in a query string verbatim.
    """
    raw = json.dumps(
        {"ts": submitted_at.isoformat(), "id": id, "fh": filter_hash}
    )
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def _decode_cursor(
    cursor: str, *, expected_filter_hash: str
) -> tuple[datetime, str]:
    """Recover the ``(submitted_at, id)`` anchor from a cursor blob.

    Raises :class:`CursorInvalidError` for malformed input and
    :class:`CursorFilterMismatchError` when the embedded filter
    hash does not match the active query — both subclass
    :class:`ValueError` so legacy ``except ValueError`` blocks keep
    working.
    """
    pad = "=" * (-len(cursor) % 4)
    try:
        obj = json.loads(base64.urlsafe_b64decode(cursor + pad))
    except Exception as exc:
        raise CursorInvalidError(f"invalid cursor: {exc}") from exc
    if not isinstance(obj, dict) or "ts" not in obj or "id" not in obj:
        raise CursorInvalidError("invalid cursor: missing ts/id fields")
    if obj.get("fh") != expected_filter_hash:
        raise CursorFilterMismatchError(
            f"cursor filter hash mismatch "
            f"(cursor={obj.get('fh')!r}, "
            f"current_filter={expected_filter_hash!r})"
        )
    try:
        ts = datetime.fromisoformat(obj["ts"])
    except (TypeError, ValueError) as exc:
        raise CursorInvalidError(
            f"invalid cursor: ts not iso8601 ({obj.get('ts')!r})"
        ) from exc
    return ts, str(obj["id"])


def _tag_filter_clause(
    tag: str, *, dialect: str
) -> sa.ColumnElement[bool]:
    """SQL-side JSON-array containment check for the ``tags`` column.

    SQLAlchemy's default ``JSON.contains()`` falls back to ``LIKE`` on
    SQLite which never matches array elements, so we branch on the
    dialect:

      * **SQLite** — ``EXISTS (SELECT 1 FROM json_each(sessions.tags)
        WHERE value = :tag_param)`` via the JSON1 extension that
        ships with stock SQLite 3.38+ (default-on in ``aiosqlite``).
      * **Postgres** — ``sessions.tags::jsonb @> jsonb_build_array(:tag)``
        via the JSONB containment operator. We cast on the fly so
        the column can stay typed as ``JSON`` (not ``JSONB``) and
        still get index-friendly containment when the deployment
        adds a GIN index.

    Other dialects raise :class:`NotImplementedError` — gg-relay only
    supports SQLite (dev/test) + Postgres (prod) per Plan 4 §8.
    """
    if dialect == "sqlite":
        return sa.text(
            "EXISTS (SELECT 1 FROM json_each(sessions.tags) AS je "
            "WHERE je.value = :tag_param)"
        ).bindparams(tag_param=tag)
    if dialect in {"postgresql", "postgres"}:
        return sa.text(
            "sessions.tags::jsonb @> jsonb_build_array(:tag_param)"
        ).bindparams(tag_param=tag)
    raise NotImplementedError(
        f"tag filter not implemented for dialect {dialect!r}"
    )


# ── Audit cursor helpers (Plan 8 D8.4) ───────────────────────────────
# Mirror :func:`_filter_hash` / :func:`_encode_cursor` /
# :func:`_decode_cursor` for the audit list endpoint. Same bind-the-
# filter-into-the-cursor pattern so paging across a filter change is
# rejected with a clear error rather than silently returning a confusing
# mix of rows. The audit anchor is ``(ts, id)`` instead of
# ``(submitted_at, id)`` because audit_log doesn't carry a session-style
# ``submitted_at`` column — its row id IS the natural insertion order
# tiebreaker on ts ties.


def _audit_filter_hash(
    *,
    actor: str | None,
    action: str | None,
    target_type: str | None,
    target_id: str | None,
) -> str:
    """Compact deterministic identifier for the active audit filter.

    SHA1-truncated identical to :func:`_filter_hash` — paging through
    audit rows with the same filter combo always reproduces the hash;
    swapping any filter shifts the hash and the next ``after=...``
    surfaces :class:`CursorFilterMismatchError`.
    """
    parts = sorted(
        f"{k}={v or '*'}"
        for k, v in {
            "actor": actor,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
        }.items()
    )
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def _encode_audit_cursor(
    *, ts: datetime, id: int, filter_hash: str
) -> str:
    raw = json.dumps({"ts": ts.isoformat(), "id": int(id), "fh": filter_hash})
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def _decode_audit_cursor(
    cursor: str, *, expected_filter_hash: str
) -> tuple[datetime, int]:
    pad = "=" * (-len(cursor) % 4)
    try:
        obj = json.loads(base64.urlsafe_b64decode(cursor + pad))
    except Exception as exc:
        raise CursorInvalidError(f"invalid audit cursor: {exc}") from exc
    if not isinstance(obj, dict) or "ts" not in obj or "id" not in obj:
        raise CursorInvalidError("invalid audit cursor: missing ts/id fields")
    if obj.get("fh") != expected_filter_hash:
        raise CursorFilterMismatchError(
            f"audit cursor filter hash mismatch "
            f"(cursor={obj.get('fh')!r}, "
            f"current_filter={expected_filter_hash!r})"
        )
    try:
        ts = datetime.fromisoformat(obj["ts"])
    except (TypeError, ValueError) as exc:
        raise CursorInvalidError(
            f"invalid audit cursor: ts not iso8601 ({obj.get('ts')!r})"
        ) from exc
    try:
        id_ = int(obj["id"])
    except (TypeError, ValueError) as exc:
        raise CursorInvalidError(
            f"invalid audit cursor: id not integer ({obj.get('id')!r})"
        ) from exc
    return ts, id_


class SqlAlchemyStore:
    """Async DAO over the persistence tables.

    Construct once with the shared :class:`AsyncEngine` and reuse across
    handlers — methods open + close a per-call connection (SQLAlchemy
    handles pooling).

    Structurally implements
    :class:`gg_relay.store.protocol.SessionStore`,
    :class:`gg_relay.store.protocol.FrameStore`,
    :class:`gg_relay.store.protocol.HITLStore`, and (Plan 8 D8.4)
    :class:`gg_relay.store.protocol.AuditStore`.
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
        owner: str | None = None,
        description: str | None = None,
        parent_session_id: str | None = None,
    ) -> None:
        """Insert a brand-new session in ``queued`` state.

        ``spec_json`` MUST already be redacted by the caller.

        Plan 7 Task 6b / D7.26 — ``owner`` and ``description`` are
        persisted as-is. Truncation of ``description`` happens at
        the router layer; the store assumes the caller has already
        applied the 512-char cap.

        Plan 8 D8.6 / Task 9 — ``parent_session_id`` (optional) marks
        the row as the retry of an earlier session. ``None`` means
        "top-level submission". The column is NOT enforced as a
        foreign key (parent may be archived/deleted) so the store
        layer accepts any 36-char string verbatim; the manager is
        the source-of-truth for "did the parent really exist".
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
                    owner=owner,
                    description=description,
                    parent_session_id=parent_session_id,
                )
            )

    async def list_children_of_session(
        self, *, parent_session_id: str
    ) -> list[RowMapping]:
        """List sessions whose ``parent_session_id`` matches the argument.

        Plan 8 D8.6 / Task 9. Powers the dashboard's retry-tree view —
        passing the original session's id walks one level down to
        every retry. Walking deeper trees (retry-of-retry) is the
        caller's responsibility (recursive CTE in the dashboard
        query layer); the store stays single-level so the
        ``ix_sessions_parent_session_id`` index seek path remains
        the dominant cost.

        Rows are ordered by ``submitted_at`` ascending so the UI can
        render the children in the order they were retried.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(sessions)
                .where(sessions.c.parent_session_id == parent_session_id)
                .order_by(sessions.c.submitted_at.asc())
            )
            return list(result.mappings().all())

    async def update_session_status(
        self,
        session_id: str,
        *,
        status: str | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        end_reason: str | None = None,
        runtime_id: str | None = None,
        paused_at: datetime | None = None,
        expected_version: int | None = None,
    ) -> int:
        """Patch the status / lifecycle columns for a session.

        Plan 7 D7.5 / Task 8 — adds optimistic locking. Behaviour:

        * Any ``None`` argument among the column kwargs is left
          untouched (no overwrite of an existing value), matching
          the pre-Task-8 contract so legacy callers keep working.
        * Every successful update bumps ``version`` by 1.
        * When ``expected_version`` is supplied, the UPDATE adds
          ``WHERE version = :expected_version`` so a stale read
          surfaces as :class:`ConcurrencyError` (rowcount == 0).
          The exception carries the actual current version so the
          caller can decide to retry (managed in SessionManager)
          or surface ``409`` to the user (HITL resolve, API
          endpoints).
        * When ``expected_version`` is ``None``, the implementation
          reads the current version under the same connection and
          bumps it blindly — backwards-compatible with every
          pre-Task-8 call site.

        Returns the new version after the update (or the row's
        current version when there is nothing to update — that
        case never bumps anything).
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
        if paused_at is not None:
            values["paused_at"] = paused_at
        if not values and expected_version is None:
            return 0
        async with self._engine.begin() as conn:
            if expected_version is None:
                current = await conn.execute(
                    select(sessions.c.version).where(
                        sessions.c.id == session_id
                    )
                )
                cur_v_raw = current.scalar()
                if cur_v_raw is None:
                    # Row doesn't exist; preserve pre-Task-8 silent no-op.
                    return 0
                cur_v = int(cur_v_raw)
                new_version = cur_v + 1
            else:
                new_version = int(expected_version) + 1
            if not values:
                return new_version - 1
            values["version"] = new_version
            where: list[sa.ColumnElement[bool]] = [sessions.c.id == session_id]
            if expected_version is not None:
                where.append(sessions.c.version == expected_version)
            result = await conn.execute(
                update(sessions).where(*where).values(**values)
            )
            if result.rowcount == 0 and expected_version is not None:
                actual = (
                    await conn.execute(
                        select(sessions.c.version).where(
                            sessions.c.id == session_id
                        )
                    )
                ).scalar()
                raise ConcurrencyError(
                    f"session {session_id} version mismatch",
                    expected_version=int(expected_version),
                    actual_version=int(actual) if actual is not None else None,
                )
        return new_version

    async def get_session_version(self, session_id: str) -> int | None:
        """Plan 7 D7.5 / Task 8 — read the optimistic-locking version.

        Returns ``None`` when the row does not exist. Callers (the
        SessionManager pause/resume retry helper) use this before
        a version-checked write so the ``expected_version`` kwarg
        is anchored to a recent read.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(sessions.c.version).where(sessions.c.id == session_id)
            )
            v = result.scalar()
        if v is None:
            return None
        return int(v)

    async def list_sessions(
        self,
        *,
        status: str | None = None,
        tag: str | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> tuple[list[RowMapping], str | None]:
        """List sessions newest-first with cursor pagination.

        Plan 7 D7.6 / Task 9. Returns ``(rows, next_cursor)`` where
        ``rows`` is up to ``limit`` rows ordered ``submitted_at`` /
        ``id`` descending (newest first; ``id`` is the stable
        tiebreaker so pages don't jitter when two rows share a
        ``submitted_at``) and ``next_cursor`` is either:

          * a urlsafe-base64 token to pass back as ``after=`` for the
            next page, OR
          * ``None`` when the current page exhausts the result set.

        The ``after`` cursor MUST have been minted under the same
        ``status`` + ``tag`` combination — :func:`_decode_cursor`
        compares the embedded filter hash and raises
        :class:`CursorFilterMismatchError` otherwise. Garbage cursors
        raise :class:`CursorInvalidError`. Routers map both to
        HTTP 400.

        ``tag`` filtering runs SQL-side via :func:`_tag_filter_clause`
        so pagination math stays correct (Python-side filtering would
        let pages drop rows and the cursor would point past them).
        """
        fh = _filter_hash(status=status, tag=tag)
        where: list[sa.ColumnElement[bool]] = []
        if status is not None:
            where.append(sessions.c.status == status)
        if tag is not None:
            where.append(
                _tag_filter_clause(tag, dialect=self._engine.dialect.name)
            )
        if after is not None:
            ts, anchor_id = _decode_cursor(after, expected_filter_hash=fh)
            where.append(
                or_(
                    sessions.c.submitted_at < ts,
                    and_(
                        sessions.c.submitted_at == ts,
                        sessions.c.id < anchor_id,
                    ),
                )
            )
        stmt = (
            select(sessions)
            .where(*where)
            .order_by(
                sessions.c.submitted_at.desc(),
                sessions.c.id.desc(),
            )
            .limit(limit + 1)
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            rows = list(result.mappings().all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor: str | None = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = _encode_cursor(
                submitted_at=last["submitted_at"],
                id=last["id"],
                filter_hash=fh,
            )
        return rows, next_cursor

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

    async def list_paused(self) -> list[RowMapping]:
        """List sessions currently in the ``paused`` state.

        Plan 7 D7.18 / Task 14 — feeds
        :func:`gg_relay.session.recovery.recover_paused_timers` so the
        startup hook can decide per row whether to re-arm the paused
        watchdog (still within the timeout window) or cancel the
        session outright (elapsed > ``paused_timeout_s``).

        Rows are returned ordered by ``paused_at`` descending so the
        most recently paused get processed first (only a tie-breaker —
        recovery treats every row independently).
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(sessions)
                .where(
                    and_(
                        sessions.c.status == "paused",
                        sessions.c.paused_at.is_not(None),
                    )
                )
                .order_by(sessions.c.paused_at.desc())
            )
            return list(result.mappings().all())

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
        expected_version: int | None = None,
    ) -> int | None:
        """Insert-or-update a HITL request row.

        Plan 7 D7.5 / Task 8 — adds optimistic locking on the UPDATE
        path. Behaviour:

        * INSERT path (no existing row) inserts ``version=0`` via the
          schema default; ``expected_version`` is ignored.
        * UPDATE path bumps ``version`` to ``current + 1``. When
          ``expected_version`` is supplied, an additional ``WHERE
          version = :expected_version`` clause makes a stale read
          surface as :class:`ConcurrencyError`. The HITL path **does
          not retry** — the API router catches this and returns 409
          with a body carrying the winning decision.

        Returns the new version on the UPDATE path, or ``None`` if
        the row was just inserted (the caller's INSERT-path use case
        — registering a new pending request — has no use for the
        version).

        Uses SQLite's ``ON CONFLICT DO UPDATE`` when the dialect
        supports it and falls back to a SELECT-then-UPDATE/INSERT for
        portability. When ``expected_version`` is supplied the path
        is always a plain UPDATE so the version-check is unambiguous
        across dialects.
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
        # Explicit version-checked UPDATE — used by the HITL resolve
        # flow once a pending row exists. Bypasses dialect-specific
        # UPSERT shapes so the WHERE-clause behaviour is uniform.
        if expected_version is not None:
            new_version = int(expected_version) + 1
            upd = {
                k: v
                for k, v in values.items()
                if k not in {"id", "session_id", "created_at"}
            }
            upd["version"] = new_version
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    update(hitl_requests)
                    .where(
                        and_(
                            hitl_requests.c.id == id,
                            hitl_requests.c.version == expected_version,
                        )
                    )
                    .values(**upd)
                )
                if result.rowcount == 0:
                    actual = (
                        await conn.execute(
                            select(hitl_requests.c.version).where(
                                hitl_requests.c.id == id
                            )
                        )
                    ).scalar()
                    raise ConcurrencyError(
                        f"hitl {id} version mismatch",
                        expected_version=int(expected_version),
                        actual_version=(
                            int(actual) if actual is not None else None
                        ),
                    )
            return new_version

        dialect = self._engine.dialect.name
        async with self._engine.begin() as conn:
            if dialect == "sqlite":
                stmt = sqlite_insert(hitl_requests).values(**values)
                upd = {
                    k: v
                    for k, v in values.items()
                    if k not in {"id", "session_id", "created_at"}
                }
                # Blind-bump version on UPDATE so even non-checked
                # upserts increment a row's optimistic-locking
                # counter (so callers that DO use expected_version
                # later see monotonic growth).
                upd["version"] = hitl_requests.c.version + 1
                stmt = stmt.on_conflict_do_update(
                    index_elements=[hitl_requests.c.id], set_=upd
                )
                await conn.execute(stmt)
                return None
            try:
                await conn.execute(insert(hitl_requests).values(**values))
            except IntegrityError:
                upd = {
                    k: v
                    for k, v in values.items()
                    if k not in {"id", "session_id", "created_at"}
                }
                upd["version"] = hitl_requests.c.version + 1
                await conn.execute(
                    update(hitl_requests)
                    .where(hitl_requests.c.id == id)
                    .values(**upd)
                )
        return None

    async def get_hitl_version(self, req_id: str) -> int | None:
        """Plan 7 D7.5 / Task 8 — read the HITL row's optimistic-locking version.

        Returns ``None`` when the row does not exist. Used by the API
        router to read the pre-resolve version before issuing a
        ``upsert_hitl(expected_version=...)`` call.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(hitl_requests.c.version).where(
                    hitl_requests.c.id == req_id
                )
            )
            v = result.scalar()
        if v is None:
            return None
        return int(v)

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

    # ── audit (Plan 8 D8.4) ───────────────────────────────────────────

    async def record_audit(
        self,
        *,
        actor: str,
        action: str,
        target_type: str | None = None,
        target_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        ts: datetime | None = None,
        conn: Any = None,
    ) -> int:
        """Append a single audit row, return the new row's ``id``.

        Plan 8 D8.4 / Task 5. ``actor`` is required (the schema CHECK
        is non-NULL); the API layer collapses unknown identities to
        ``"anon"`` so the column is always populated.

        ``conn`` (optional) is an externally-managed
        :class:`sqlalchemy.ext.asyncio.AsyncConnection`. When supplied
        the INSERT runs on that connection without opening a new
        transaction so the caller can wrap the audit write inside the
        same transaction as the business mutation it audits — that's
        the v2.1 MAJOR 3 durable-outbox pattern. Without ``conn`` the
        method opens its own short-lived transaction; that's the
        fallback path used by :class:`AuditFallbackMiddleware` (no
        access to the manager's transaction).

        ``metadata`` is stored verbatim; callers MUST pre-redact any
        sensitive values (the surrounding store code never inspects
        the dict).
        """
        if ts is None:
            ts = _utcnow()
        values = {
            "ts": ts,
            "actor": actor,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "metadata_json": dict(metadata) if metadata else None,
            "request_id": request_id,
        }
        # ``RETURNING id`` is the canonical Postgres path; SQLite needs
        # the ``inserted_primary_key`` lastrowid fallback because the
        # bundled sqlite3 in the test image predates SQLite 3.35
        # RETURNING support. We branch on the dialect rather than
        # try/except so the happy path stays cheap.
        dialect = self._engine.dialect.name
        if dialect in {"postgresql", "postgres"}:
            stmt = (
                insert(audit_log).values(**values).returning(audit_log.c.id)
            )
            if conn is not None:
                result = await conn.execute(stmt)
                row = result.first()
                return int(row[0]) if row is not None else 0
            async with self._engine.begin() as new_conn:
                result = await new_conn.execute(stmt)
                row = result.first()
                return int(row[0]) if row is not None else 0
        # SQLite (and any other dialect that lacks RETURNING) — read
        # the autoincrement id from ``inserted_primary_key`` after the
        # INSERT executes. SQLAlchemy populates that on the
        # CursorResult regardless of dialect.
        stmt = insert(audit_log).values(**values)
        if conn is not None:
            result = await conn.execute(stmt)
            pk = result.inserted_primary_key
            return int(pk[0]) if pk and pk[0] is not None else 0
        async with self._engine.begin() as new_conn:
            result = await new_conn.execute(stmt)
            pk = result.inserted_primary_key
            return int(pk[0]) if pk and pk[0] is not None else 0

    async def list_audit(
        self,
        *,
        session_id: str | None = None,
        actor: str | None = None,
        action: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> tuple[list[RowMapping], str | None]:
        """List audit rows newest-first with cursor pagination.

        Plan 8 D8.4 / Task 5. Cursor semantics mirror
        :meth:`list_sessions` (Plan 7 D7.6) — see
        :func:`_audit_filter_hash` / :func:`_encode_audit_cursor` for
        the cursor binding contract. Order is ``ts DESC, id DESC`` so
        ties on ``ts`` (multiple writes inside the same Postgres
        microsecond) page deterministically.

        ``session_id`` is a convenience alias that auto-resolves to
        ``target_type='session'`` + ``target_id=<sid>``. Passing both
        ``session_id`` and an explicit ``target_type`` / ``target_id``
        kwarg lets the explicit values win — useful when the caller
        wants to audit non-session targets (HITL row, comment row,
        …) under the same listing surface.
        """
        if session_id is not None:
            if target_type is None:
                target_type = "session"
            if target_id is None:
                target_id = session_id
        fh = _audit_filter_hash(
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
        )
        where: list[sa.ColumnElement[bool]] = []
        if actor is not None:
            where.append(audit_log.c.actor == actor)
        if action is not None:
            where.append(audit_log.c.action == action)
        if target_type is not None:
            where.append(audit_log.c.target_type == target_type)
        if target_id is not None:
            where.append(audit_log.c.target_id == target_id)
        if after is not None:
            ts_after, id_after = _decode_audit_cursor(
                after, expected_filter_hash=fh
            )
            where.append(
                or_(
                    audit_log.c.ts < ts_after,
                    and_(
                        audit_log.c.ts == ts_after,
                        audit_log.c.id < id_after,
                    ),
                )
            )
        stmt = (
            select(audit_log)
            .where(*where)
            .order_by(audit_log.c.ts.desc(), audit_log.c.id.desc())
            .limit(limit + 1)
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            rows = list(result.mappings().all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor: str | None = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = _encode_audit_cursor(
                ts=last["ts"], id=int(last["id"]), filter_hash=fh
            )
        return rows, next_cursor


    # ── session_comments (Plan 8 D8.5 / Task 7) ───────────────────────

    async def create_comment(
        self,
        *,
        session_id: str,
        author: str,
        body_markdown: str,
        body_html: str,
        conn: Any = None,
    ) -> dict[str, Any]:
        """Insert one comment row; return the materialised row dict.

        Plan 8 D8.5 / Task 7. ``body_html`` MUST already be sanitised
        by :func:`gg_relay.comments.sanitizer.render_safe` — the
        store does not inspect HTML for XSS. ``body_markdown`` is
        kept verbatim so a future sanitizer upgrade can re-render
        historical rows.

        The same-tx ``conn`` kwarg follows the v2.1 MAJOR 3
        durable-outbox pattern: the router wraps the
        :meth:`AuditService.record` call inside the same
        ``engine.begin()`` block so the comment + its audit row
        commit or roll back atomically. Without ``conn`` the method
        opens its own short-lived transaction.

        The returned dict mirrors the on-disk row shape so callers
        can hand it straight to the response serializer without a
        follow-up ``get_comment`` round trip.
        """
        now = _utcnow()
        stmt = insert(session_comments).values(
            session_id=session_id,
            author=author,
            body_markdown=body_markdown,
            body_html=body_html,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        if conn is not None:
            result = await conn.execute(stmt)
        else:
            async with self._engine.begin() as new_conn:
                result = await new_conn.execute(stmt)
        pk = result.inserted_primary_key
        cid = int(pk[0]) if pk and pk[0] is not None else 0
        return {
            "id": cid,
            "session_id": session_id,
            "author": author,
            "body_markdown": body_markdown,
            "body_html": body_html,
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
        }

    async def list_comments(
        self,
        *,
        session_id: str,
        include_deleted: bool = False,
        limit: int = 100,
    ) -> list[RowMapping]:
        """List comments for one session, oldest first.

        Plan 8 D8.5 / Task 7. Tied to the
        ``ix_session_comments_session_created`` composite index so
        the canonical "render the discussion thread top-to-bottom"
        query is one seek + a sequential scan over a small range.

        Soft-deleted rows (``deleted_at IS NOT NULL``) are excluded
        by default; the moderation path passes ``include_deleted=True``
        to retrieve the tombstoned rows for review.
        """
        where: list[sa.ColumnElement[bool]] = [
            session_comments.c.session_id == session_id,
        ]
        if not include_deleted:
            where.append(session_comments.c.deleted_at.is_(None))
        stmt = (
            select(session_comments)
            .where(*where)
            .order_by(session_comments.c.created_at.asc())
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            return list(result.mappings().all())

    async def get_comment(
        self, *, comment_id: int
    ) -> RowMapping | None:
        """Fetch a single comment row by id (incl. tombstoned).

        Plan 8 D8.5 / Task 7. The router uses this for the
        author / admin authorization check on PATCH / DELETE, so
        soft-deleted rows are returned and the caller filters on
        ``row["deleted_at"]``.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(session_comments).where(
                    session_comments.c.id == comment_id
                )
            )
            return result.mappings().first()

    async def update_comment(
        self,
        *,
        comment_id: int,
        body_markdown: str,
        body_html: str,
        conn: Any = None,
    ) -> bool:
        """Update comment body; bump ``updated_at``. Returns ``True``
        when a row was modified.

        Plan 8 D8.5 / Task 7. Refuses to touch a soft-deleted row
        (``WHERE deleted_at IS NULL``); a concurrent delete therefore
        manifests as ``ok=False`` and the router maps it to a 404.

        ``conn`` follows the same-tx outbox pattern documented on
        :meth:`create_comment`.
        """
        stmt = (
            update(session_comments)
            .where(
                session_comments.c.id == comment_id,
                session_comments.c.deleted_at.is_(None),
            )
            .values(
                body_markdown=body_markdown,
                body_html=body_html,
                updated_at=_utcnow(),
            )
        )
        if conn is not None:
            result = await conn.execute(stmt)
            return int(result.rowcount or 0) > 0
        async with self._engine.begin() as new_conn:
            result = await new_conn.execute(stmt)
            return int(result.rowcount or 0) > 0

    async def soft_delete_comment(
        self, *, comment_id: int, conn: Any = None
    ) -> bool:
        """Tombstone a comment by stamping ``deleted_at = utcnow``.

        Plan 8 D8.5 / Task 7. Idempotent: a second soft-delete on the
        same id returns ``False`` because the ``deleted_at IS NULL``
        WHERE clause already excludes the tombstoned row. Callers
        (the DELETE endpoint) map ``False`` to 404 so concurrent
        deletes converge on the same status.

        ``conn`` follows the same-tx outbox pattern documented on
        :meth:`create_comment`.
        """
        stmt = (
            update(session_comments)
            .where(
                session_comments.c.id == comment_id,
                session_comments.c.deleted_at.is_(None),
            )
            .values(deleted_at=_utcnow())
        )
        if conn is not None:
            result = await conn.execute(stmt)
            return int(result.rowcount or 0) > 0
        async with self._engine.begin() as new_conn:
            result = await new_conn.execute(stmt)
            return int(result.rowcount or 0) > 0


class SessionRepository(SqlAlchemyStore):
    """Deprecated alias for :class:`SqlAlchemyStore`.

    Renamed in Plan 7 Task 5 (D7.4); the alias will be removed in
    0.8.0. Construct :class:`SqlAlchemyStore` directly instead.

    The warning fires only on **instantiation** so importing
    :mod:`gg_relay.store` (or this module) stays silent. ``isinstance``
    against :class:`SqlAlchemyStore` and any of the
    :mod:`gg_relay.store.protocol` Protocols still resolves to ``True``
    because this is a thin subclass.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warnings.warn(
            "SessionRepository has been renamed to SqlAlchemyStore "
            "(gg_relay.store.SqlAlchemyStore); the alias will be removed "
            "in 0.8.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(*args, **kwargs)
