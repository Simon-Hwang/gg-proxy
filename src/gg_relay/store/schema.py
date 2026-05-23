"""SQLAlchemy Core metadata for gg-relay persistence layer.

Three tables:

- ``sessions``      — per-submission row (status, redacted spec, lifecycle ts)
- ``frames``        — append-only EventFrame stream (redacted payload)
- ``hitl_requests`` — pending/resolved HITL decisions (redacted args)

All ``JSON`` columns store **already-redacted** dicts; the RedactionEngine
runs at the SessionManager boundary so the store layer never sees raw
credentials. SQLite supports ``JSON`` natively (stored as ``TEXT``); on
Postgres SQLAlchemy maps to ``JSONB`` transparently.
"""
from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

# SQLite has a single INTEGER type and only ROWID columns auto-increment.
# BigInteger autoincrement on SQLite breaks ("NOT NULL constraint failed:
# frames.id") because SQLAlchemy doesn't map BigInteger to ROWID. The
# ``with_variant`` keeps Postgres on real BIGINT while letting SQLite use
# its native ROWID-backed INTEGER PK.
_PK_BIG = BigInteger().with_variant(Integer(), "sqlite")

metadata = MetaData()

sessions = Table(
    "sessions",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("status", String(16), nullable=False),
    Column("spec_json", JSON, nullable=False),
    Column("tags", JSON, nullable=False, default=list),
    Column("submitted_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("end_reason", String(128), nullable=True),
    Column("trace_id", String(32), nullable=True),
    Column("backend", String(16), nullable=False),
    Column("runtime_id", String(64), nullable=True),
    # ── Plan 6 D6.12: per-session aggregates ─────────────────────────
    # Populated by SessionManager._record_session_end (see Task 8).
    # Defaults to 0 so existing rows (Plan 4/5) and any rows written
    # before the SessionManager hook upgrade still satisfy NOT NULL.
    Column(
        "input_tokens",
        BigInteger,
        nullable=False,
        server_default="0",
        default=0,
    ),
    Column(
        "output_tokens",
        BigInteger,
        nullable=False,
        server_default="0",
        default=0,
    ),
    Column(
        "cost_usd",
        Float,
        nullable=False,
        server_default="0",
        default=0.0,
    ),
    Column(
        "turn_count",
        Integer,
        nullable=False,
        server_default="0",
        default=0,
    ),
    # ── Plan 7 D7.5: optimistic locking + pause/resume watchdog ──────
    # ``version`` is bumped on every state transition so concurrent
    # writers detect lost updates (Plan 7 Task 8). Existing rows are
    # populated to 0 by Alembic 0003's ``server_default``; new rows
    # default to 0 from the Python-side ``default`` so SQLAlchemy emits
    # the value even when the column is omitted from an INSERT.
    Column(
        "version",
        Integer,
        nullable=False,
        server_default="0",
        default=0,
    ),
    # ``paused_at`` is set when the session enters ``paused`` and
    # cleared on resume; the pause-timeout watchdog filters by
    # ``paused_at < cutoff`` to auto-cancel sessions that exceed the
    # configured cap.
    Column("paused_at", DateTime(timezone=True), nullable=True),
    # ── Plan 7 D7.26: single-team multi-maintainer collaboration ─────
    # ``owner`` is auto-attributed from the API key's label
    # (``request.state.api_key_label`` set by ``APIKeyAuthMiddleware``)
    # so existing clients gain attribution without code changes. The
    # dashboard / Kanban "filter by owner" predicate is hot — index
    # for an equality scan. ``description`` is a short free-form
    # annotation; the router truncates to 512 chars and surfaces
    # ``X-Description-Truncated: true`` so the store sees at most
    # 512 chars and never has to truncate itself.
    Column("owner", String(64), nullable=True),
    Column("description", String(512), nullable=True),
    # ── Plan 6 D6.12: completed_at index for time-bucketed chart queries.
    # Reuses the existing ``ended_at`` column — every terminal-state
    # transition writes both ``ended_at`` AND ``status`` so the new
    # global-chart aggregator can filter on ``ended_at >= cutoff`` and
    # group by bucket without a separate "completed_at" column. The
    # index name follows Plan 6 §6.12 wording.
    Index("ix_sessions_status", "status"),
    Index("ix_sessions_trace_id", "trace_id"),
    Index("ix_sessions_submitted_at", "submitted_at"),
    Index("ix_sessions_completed_at", "ended_at"),
    Index("ix_sessions_owner", "owner"),
)

frames = Table(
    "frames",
    metadata,
    Column("id", _PK_BIG, primary_key=True, autoincrement=True),
    Column(
        "session_id",
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("seq", Integer, nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("type", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint("session_id", "seq", name="uq_frames_session_seq"),
    Index("ix_frames_session_id", "session_id"),
    Index("ix_frames_ts", "ts"),
)

hitl_requests = Table(
    "hitl_requests",
    metadata,
    Column("id", String(96), primary_key=True),
    Column(
        "session_id",
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("tool", String(64), nullable=False),
    Column("args_json", JSON, nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    Column("reason", String(256), nullable=True),
    Column("resolver", String(96), nullable=True),
    # ── Plan 7 D7.5: optimistic locking version ──────────────────────
    # Same semantics as ``sessions.version`` — bumped on every status
    # transition so concurrent ``resolve`` attempts detect the loser
    # and surface ``HITLAlreadyResolved`` (Plan 7 Task 8).
    Column(
        "version",
        Integer,
        nullable=False,
        server_default="0",
        default=0,
    ),
    Index("ix_hitl_status", "status"),
    Index("ix_hitl_session", "session_id"),
)

# ── Plan 7 D7.17: append-only durable event store (Task 7) ───────────
# Backs the optional disk-tier of ``AsyncEventBus``. Plan 7 Task 13
# wires ``DurableEventStore`` Protocol to write here; this table only
# provisions storage so the bus implementation can land independently.
#
# No foreign keys — events are append-only telemetry. A delete-session
# cascade would wipe audit history, which is the opposite of what a
# durable bus needs. Operators prune via a TTL job (Plan 8) instead.
events = Table(
    "events",
    metadata,
    Column("event_id", String(36), primary_key=True),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("type", String(50), nullable=False),
    Column("session_id", String(36), nullable=True),
    Column("payload", JSON, nullable=False),
    # ``in_process`` | ``disk`` | ``redis`` (Plan 8 adds the Redis tier;
    # Plan 7 only emits in_process | disk).
    Column("delivery_tier", String(10), nullable=False),
    # ``ix_events_ts`` powers range-scan replay (e.g. "events since T").
    # ``ix_events_session_id`` powers per-session replay.
    Index("ix_events_ts", "ts"),
    Index("ix_events_session_id", "session_id"),
)

# ── Plan 8 D8.4 (Task 5): durable audit log for sensitive mutations ──
# Every business mutation (session create / cancel / pause / resume /
# delete) writes a row here, ideally in the same transaction as the
# business update (durable outbox pattern; v2.1 MAJOR 3). Routes that
# forgot fall back to :class:`AuditFallbackMiddleware`, which writes an
# ``unknown_mutation`` row fire-and-forget after the response is sent.
#
# Indexes chosen to power the three canonical queries the dashboard and
# the upcoming Plan 8 audit endpoint need:
#   * ``ix_audit_log_ts``         — newest-first global scan
#   * ``ix_audit_log_actor_ts``   — "every action by alice"
#   * ``ix_audit_log_target``     — composite ``(target_type, target_id)``
#                                   for "audit history of session sid-xyz"
#
# ``id`` is a plain ``Integer`` autoincrement PK — audit volume is bounded
# by API mutation rate (small) so we don't pay the Postgres BIGINT cost.
# ``metadata_json`` mirrors the redacted-JSON convention used by other
# tables: callers MUST pre-redact before passing values down.
audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("actor", String(64), nullable=False),
    Column("action", String(64), nullable=False),
    Column("target_type", String(32), nullable=True),
    Column("target_id", String(128), nullable=True),
    Column("metadata_json", JSON, nullable=True),
    Column("request_id", String(36), nullable=True),
    Index("ix_audit_log_ts", "ts"),
    Index("ix_audit_log_actor_ts", "actor", "ts"),
    Index("ix_audit_log_target", "target_type", "target_id"),
)
