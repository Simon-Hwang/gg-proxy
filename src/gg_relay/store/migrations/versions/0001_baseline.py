"""baseline.

Revision ID: 0001
Revises:
Create Date: 2026-05-22

Plan 4 Task 1 — the three persistence tables (sessions / frames /
hitl_requests). The schema is the source of truth in
``gg_relay.store.schema``; this migration is the database-side mirror so
``alembic upgrade head`` is the canonical way to provision a Postgres prod
deployment.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("spec_json", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("end_reason", sa.String(length=128)),
        sa.Column("trace_id", sa.String(length=32)),
        sa.Column("backend", sa.String(length=16), nullable=False),
        sa.Column("runtime_id", sa.String(length=64)),
    )
    op.create_index("ix_sessions_status", "sessions", ["status"])
    op.create_index("ix_sessions_trace_id", "sessions", ["trace_id"])
    op.create_index(
        "ix_sessions_submitted_at", "sessions", ["submitted_at"]
    )

    op.create_table(
        "frames",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "session_id", "seq", name="uq_frames_session_seq"
        ),
    )
    op.create_index("ix_frames_session_id", "frames", ["session_id"])
    op.create_index("ix_frames_ts", "frames", ["ts"])

    op.create_table(
        "hitl_requests",
        sa.Column("id", sa.String(length=96), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool", sa.String(length=64), nullable=False),
        sa.Column("args_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String(length=256)),
        sa.Column("resolver", sa.String(length=96)),
    )
    op.create_index("ix_hitl_status", "hitl_requests", ["status"])
    op.create_index("ix_hitl_session", "hitl_requests", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_hitl_session", table_name="hitl_requests")
    op.drop_index("ix_hitl_status", table_name="hitl_requests")
    op.drop_table("hitl_requests")
    op.drop_index("ix_frames_ts", table_name="frames")
    op.drop_index("ix_frames_session_id", table_name="frames")
    op.drop_table("frames")
    op.drop_index("ix_sessions_submitted_at", table_name="sessions")
    op.drop_index("ix_sessions_trace_id", table_name="sessions")
    op.drop_index("ix_sessions_status", table_name="sessions")
    op.drop_table("sessions")
