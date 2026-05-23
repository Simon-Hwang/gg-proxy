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
        owner: str | None = None,
        description: str | None = None,
        parent_session_id: str | None = None,
    ) -> None:
        """Insert a new session row in ``queued`` state.

        Plan 7 Task 6b / D7.26 — ``owner`` and ``description`` are
        new optional kwargs for single-team multi-maintainer
        collaboration. Both default to ``None`` so pre-D7.26 callers
        keep working. The API router auto-attributes ``owner`` from
        ``request.state.api_key_label`` when the client doesn't
        pass one explicitly; ``description`` is truncated to 512
        chars at the router layer (the store assumes it's already
        short enough).

        Plan 8 D8.6 / Task 9 — ``parent_session_id`` (optional)
        marks the row as the retry of an earlier session. ``None``
        means "top-level submission". The column is NOT enforced as
        a foreign key so children may outlive an archived parent;
        the manager validates parent existence before submitting.
        """
        ...

    async def list_children_of_session(
        self, *, parent_session_id: str
    ) -> Sequence[Mapping[str, Any]]:
        """List sessions whose ``parent_session_id`` matches the argument.

        Plan 8 D8.6 / Task 9. Powers the dashboard's retry-tree view —
        feeding the original session's id walks one level down to
        every retry. Returned rows are ordered by ``submitted_at``
        ascending so retries appear in chronological order.
        """
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

    async def search_sessions(
        self,
        *,
        q: str | None = None,
        owner: str | None = None,
        tags: list[str] | None = None,
        status: list[str] | None = None,
        after_ts: datetime | None = None,
        before_ts: datetime | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> tuple[Sequence[Mapping[str, Any]], str | None]:
        """Search sessions with combined filters + cursor pagination.

        Plan 8 D8.20 / Task 12. Like :meth:`list_sessions` but with the
        wider filter surface required by the search endpoint and
        dashboard: ``q`` LIKE-matches the JSON ``spec_json`` payload
        (case-insensitive), ``owner`` is exact-equality, ``tags`` /
        ``status`` are OR-of-any lists, and ``after_ts`` / ``before_ts``
        bracket ``submitted_at``. Cursor semantics mirror
        :meth:`list_sessions` — invalid or filter-mismatched cursors
        raise the same ``CursorInvalidError`` /
        ``CursorFilterMismatchError`` so router error mapping stays
        uniform.
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

    async def list_paused(self) -> Sequence[Mapping[str, Any]]:
        """List every ``paused`` session row with a non-null ``paused_at``.

        Plan 7 D7.18 / Task 14. Used by
        :func:`gg_relay.session.recovery.recover_paused_timers` at
        startup to re-arm or cancel the paused-timeout watchdog for
        sessions that were paused before the previous process exit.

        Returns rows newest-first by ``paused_at`` so the recovery
        loop processes the most recently paused first (a marginal
        optimisation — startup recovery typically touches a handful
        of rows so ordering doesn't materially affect cost).
        """
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
class AuditStore(Protocol):
    """Async DAO surface for the ``audit_log`` table (Plan 8 D8.4).

    Implemented by :class:`gg_relay.store.repository.SqlAlchemyStore`.
    Captures every sensitive mutation as an immutable audit row so
    operators (and the upcoming dashboard audit panel) can answer
    "who did what when". The :class:`gg_relay.api.audit_service.AuditService`
    is the canonical entry point — business code calls
    :meth:`AuditService.record` rather than reaching into the store
    directly so the durable-outbox semantics (same-tx write via the
    optional ``conn`` kwarg) stay encapsulated.
    """

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

        ``conn`` (optional) is an externally-managed
        :class:`sqlalchemy.ext.asyncio.AsyncConnection`; when supplied
        the INSERT runs on that connection so the caller can wrap the
        audit write inside the same transaction as the business
        mutation it audits (durable outbox; v2.1 MAJOR 3). Without
        ``conn`` the implementation opens its own short-lived
        transaction — the fallback path used by the middleware.
        """
        ...

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
    ) -> tuple[Sequence[Mapping[str, Any]], str | None]:
        """List audit rows newest-first with cursor pagination.

        Mirrors :meth:`SessionStore.list_sessions` (Plan 7 D7.6) — the
        cursor is bound to the active filter combination via a short
        hash so paging across a filter change is rejected with
        :class:`gg_relay.store.exceptions.CursorFilterMismatchError`
        rather than silently returning surprise rows.

        ``session_id`` is a convenience alias for the common
        ``target_type='session'`` + ``target_id=<sid>`` query and
        composes with the explicit ``target_type`` / ``target_id``
        kwargs (when both are supplied the explicit values win).
        """
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


@runtime_checkable
class CommentStore(Protocol):
    """Async DAO surface for the ``session_comments`` table (Plan 8 D8.5).

    Implemented by :class:`gg_relay.store.repository.SqlAlchemyStore`.
    Backs the comment CRUD endpoints in
    :mod:`gg_relay.api.routers.comments` and the Task-8 dashboard
    comment stream.

    Soft-delete semantics: ``soft_delete_comment`` sets ``deleted_at``;
    ``list_comments`` filters out soft-deleted rows by default. Hard
    delete is not exposed — the moderation trail survives in
    ``session_comments`` itself, and ``audit_log`` retains the
    ``comment_delete`` action.

    All ``conn`` kwargs follow the v2.1 MAJOR 3 durable-outbox pattern:
    when the caller already has an open transaction (e.g. wrapping
    the comment write together with an :meth:`AuditStore.record_audit`
    call), passing ``conn=`` reuses that transaction so the two
    writes commit or roll back together. Without ``conn`` the method
    opens its own short-lived transaction.
    """

    async def create_comment(
        self,
        *,
        session_id: str,
        author: str,
        body_markdown: str,
        body_html: str,
        conn: Any = None,
    ) -> Mapping[str, Any]:
        """Insert a comment row; return the full row dict including
        ``id``, ``created_at``, ``updated_at``, ``deleted_at=None``.

        ``body_html`` MUST already be sanitised by the caller
        (:func:`gg_relay.comments.sanitizer.render_safe`) — the store
        never inspects HTML for XSS payloads.
        """
        ...

    async def list_comments(
        self,
        *,
        session_id: str,
        include_deleted: bool = False,
        limit: int = 100,
    ) -> Sequence[Mapping[str, Any]]:
        """List comments for one session, oldest first.

        ``include_deleted=False`` (default) hides soft-deleted rows.
        The list is capped at ``limit`` rows; pagination cursors are
        not exposed (per-session threads are bounded by UX).
        """
        ...

    async def get_comment(
        self, *, comment_id: int
    ) -> Mapping[str, Any] | None:
        """Fetch a single comment row by id, or ``None`` if absent.

        Soft-deleted rows are returned (caller filters); the moderation
        path needs to read the tombstoned row.
        """
        ...

    async def update_comment(
        self,
        *,
        comment_id: int,
        body_markdown: str,
        body_html: str,
        conn: Any = None,
    ) -> bool:
        """Update ``body_markdown`` + ``body_html`` + ``updated_at``.

        Refuses to touch a soft-deleted row (the UPDATE adds
        ``deleted_at IS NULL``). Returns ``True`` on success,
        ``False`` if no live row matched.
        """
        ...

    async def soft_delete_comment(
        self, *, comment_id: int, conn: Any = None
    ) -> bool:
        """Tombstone a comment by stamping ``deleted_at`` to ``utcnow``.

        Idempotent: a second soft-delete on the same id returns
        ``False`` (the ``deleted_at IS NULL`` WHERE clause already
        excludes the tombstoned row).
        """
        ...


@runtime_checkable
class FavoriteStore(Protocol):
    """Async DAO surface for the ``session_favorites`` table (Plan 8 D8.21).

    Implemented by :class:`gg_relay.store.repository.SqlAlchemyStore`.
    Backs the star / un-star endpoints in
    :mod:`gg_relay.api.routers.sessions` and the dashboard
    ``/dashboard/favorites`` page (Task 13).

    Idempotency contract: ``add_favorite`` and ``remove_favorite`` both
    return ``True`` only when the underlying state actually changed.
    The router uses this signal to decide whether to write the
    matching ``session_star`` / ``session_unstar`` audit row, so a
    double-star (or double-unstar) collapses to a no-op without
    polluting the audit timeline.
    """

    async def add_favorite(
        self,
        *,
        session_id: str,
        user_label: str,
        conn: Any = None,
    ) -> bool:
        """Star a session. Returns ``True`` on new row, ``False`` when
        already starred.

        Implementation collapses the unique-constraint
        :class:`sqlalchemy.exc.IntegrityError` raised on a duplicate
        ``(session_id, user_label)`` pair into ``False`` so the caller
        sees a deterministic idempotent contract regardless of dialect.
        """
        ...

    async def remove_favorite(
        self,
        *,
        session_id: str,
        user_label: str,
        conn: Any = None,
    ) -> bool:
        """Un-star a session. Returns ``True`` on actual delete,
        ``False`` when the row was not present.

        A second un-star on the same pair returns ``False`` without
        raising — the moderation path can rely on this for ``204``
        responses on repeated DELETE calls.
        """
        ...

    async def is_favorited(
        self, *, session_id: str, user_label: str
    ) -> bool:
        """Return ``True`` iff ``(session_id, user_label)`` is starred.

        Used by the dashboard kanban renderer to decide whether to
        paint the star icon in the "starred" state.
        """
        ...

    async def list_favorites(
        self, *, user_label: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List favorites for ``user_label`` most-recent first.

        Returns a list of dicts shaped like::

            [
                {
                    "favorite_id": int,
                    "session_id": str,
                    "starred_at": datetime,
                    "session": Mapping[str, Any],
                },
                ...
            ]

        The ``session`` payload is the materialised row from
        ``sessions`` for the matching ``session_id`` (the dashboard
        and the API list endpoint both need the prompt / owner /
        status alongside the favorite-id). Sessions that have since
        been deleted are silently dropped — the FK ``ON DELETE
        CASCADE`` keeps the favorite row in sync, but tests that
        bypass the cascade should still get a tidy response.
        """
        ...


__all__ = [
    "AuditStore",
    "CommentStore",
    "FavoriteStore",
    "FrameStore",
    "HITLStore",
    "SessionStore",
]
