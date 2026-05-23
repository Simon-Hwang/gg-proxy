"""Storage Protocols — Plan 7 D7.4 (Task 5).

Three :class:`typing.Protocol` surfaces describe the async persistence
operations gg-relay needs, split by aggregate root:

* :class:`SessionStore` — the ``sessions`` table CRUD + lifecycle +
  per-session aggregates used by SessionManager and the dashboard.
* :class:`FrameStore` — append-only ``frames`` ring (with pruning).
* :class:`HITLStore` — pending / resolved HITL request rows.

The Protocols are :func:`typing.runtime_checkable` so callers (e.g. tests
or alternative backends) can assert ``isinstance(store, SessionStore)``
without import-coupling to :class:`gg_relay.store.repository.SqlAlchemyStore`.

Signatures here **mirror the current SqlAlchemyStore method shape** — no
new fields are introduced. Plan 7 Task 6b (D7.26) is responsible for
adding ``owner`` and ``description`` to ``create_session``.

Note on return types: SqlAlchemyStore returns SQLAlchemy ``RowMapping``;
the Protocols use the abstract :class:`collections.abc.Mapping` so
alternative implementations are not forced to depend on SQLAlchemy.
``RowMapping`` is structurally a ``Mapping[str, Any]``.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SessionStore(Protocol):
    """Async DAO surface for the ``sessions`` table.

    Implemented by :class:`gg_relay.store.repository.SqlAlchemyStore`.
    """

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
        """Insert a new session row in ``queued`` state."""
        ...

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
        """Patch lifecycle columns (leave ``None`` args untouched).

        Plan 7 D7.5 / Task 8 — adds optimistic locking via the
        ``expected_version`` kwarg and the ``paused_at`` column kwarg
        (used by pause/resume). Returns the new ``version`` after the
        update; raises
        :class:`gg_relay.store.exceptions.ConcurrencyError` when
        ``expected_version`` is supplied and the row's current
        version no longer matches.
        """
        ...

    async def get_session_version(
        self, session_id: str
    ) -> int | None:
        """Plan 7 D7.5 — return the session's optimistic-locking version."""
        ...

    async def list_sessions(
        self,
        *,
        status: str | None = None,
        tag: str | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> tuple[Sequence[Mapping[str, Any]], str | None]:
        """List sessions newest-first with cursor pagination.

        Plan 7 D7.6 / Task 9. Returns ``(rows, next_cursor)`` where
        ``next_cursor`` is ``None`` once the result set is exhausted.
        The ``after`` cursor MUST come from a previous call against
        the same ``status`` + ``tag`` combination; alternative
        implementations should raise
        :class:`gg_relay.store.exceptions.CursorInvalidError` /
        :class:`gg_relay.store.exceptions.CursorFilterMismatchError`
        to keep the API router's 400-response path uniform.
        """
        ...

    async def get_session(
        self, session_id: str
    ) -> Mapping[str, Any] | None:
        """Fetch a single session row, or ``None`` if absent."""
        ...

    async def delete_session(self, session_id: str) -> None:
        """Delete a session row (cascades to frames + hitl_requests)."""
        ...

    async def update_session_aggregates(
        self,
        session_id: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        turn_count: int = 0,
    ) -> None:
        """Plan 6 D6.12 — write per-session token / cost aggregates."""
        ...

    async def aggregate_tokens_by_bucket(
        self,
        *,
        window_s: int,
        bucket_s: int,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Plan 6 D6.12 — bucketed token / cost time-series."""
        ...

    async def mark_in_flight_as_interrupted(self) -> list[str]:
        """Move every ``running`` row to ``interrupted`` (recovery)."""
        ...


@runtime_checkable
class FrameStore(Protocol):
    """Async DAO surface for the append-only ``frames`` table."""

    async def append_frame(
        self,
        session_id: str,
        *,
        seq: int,
        ts: datetime,
        type_: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Append a single frame (caller pre-redacts ``payload``)."""
        ...

    async def list_frames(
        self,
        session_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Mapping[str, Any]]:
        """List frames for a session in ``seq`` ascending order."""
        ...

    async def prune_frames_older_than(self, *, cutoff: datetime) -> int:
        """Delete frames with ``ts < cutoff``; return rows removed."""
        ...


@runtime_checkable
class HITLStore(Protocol):
    """Async DAO surface for the ``hitl_requests`` table."""

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

        Plan 7 D7.5 / Task 8 — when ``expected_version`` is supplied
        the row is updated with a ``WHERE version = :expected_version``
        clause and the implementation raises
        :class:`gg_relay.store.exceptions.ConcurrencyError` on a
        stale match. Returns the new version on update, ``None`` on
        plain insert.
        """
        ...

    async def get_hitl(
        self, req_id: str
    ) -> Mapping[str, Any] | None:
        """Fetch a single HITL request row, or ``None`` if absent."""
        ...

    async def get_hitl_version(
        self, req_id: str
    ) -> int | None:
        """Plan 7 D7.5 — return the HITL row's optimistic-locking version."""
        ...

    async def list_pending_hitl(
        self, *, session_id: str | None = None
    ) -> Sequence[Mapping[str, Any]]:
        """List ``pending`` HITL requests, optionally filtered."""
        ...


__all__ = ["FrameStore", "HITLStore", "SessionStore"]
