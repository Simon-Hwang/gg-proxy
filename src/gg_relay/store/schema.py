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
    Index("ix_sessions_status", "status"),
    Index("ix_sessions_trace_id", "trace_id"),
    Index("ix_sessions_submitted_at", "submitted_at"),
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
    Index("ix_hitl_status", "status"),
    Index("ix_hitl_session", "session_id"),
)
