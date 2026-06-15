"""trace_invocations — Plan 10 SDK hook trace table.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-10
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trace_invocations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=True),
        sa.Column("tool_use_id", sa.String(length=128), nullable=True),
        sa.Column("parent_tool_use_id", sa.String(length=128), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("input_redacted", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_trace_invocations_session",
        "trace_invocations",
        ["session_id"],
    )
    op.create_index(
        "ix_trace_invocations_session_seq",
        "trace_invocations",
        ["session_id", "seq"],
    )
    op.create_index(
        "ix_trace_invocations_tool",
        "trace_invocations",
        ["tool_name"],
    )
    op.create_index(
        "ix_trace_invocations_parent",
        "trace_invocations",
        ["parent_tool_use_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_trace_invocations_parent", table_name="trace_invocations")
    op.drop_index("ix_trace_invocations_tool", table_name="trace_invocations")
    op.drop_index(
        "ix_trace_invocations_session_seq",
        table_name="trace_invocations",
    )
    op.drop_index("ix_trace_invocations_session", table_name="trace_invocations")
    op.drop_table("trace_invocations")
