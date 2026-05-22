"""add session aggregates.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-22

Plan 6 Task 8 / D6.12 — adds the four per-session aggregate columns
(``input_tokens``, ``output_tokens``, ``cost_usd``, ``turn_count``) that
the dashboard's global + per-session charts read, plus the
``ix_sessions_completed_at`` index for time-bucketed queries.

Compatibility:
  * **SQLite + Postgres** — uses ``server_default='0'`` so existing
    rows are populated automatically by the ALTER without a separate
    UPDATE pass. Both dialects accept the literal '0' as a default
    for INTEGER / BIGINT / FLOAT columns.
  * ``nullable=False`` is safe AFTER the server_default fills existing
    rows.

Downgrade drops the index first, then the four columns. SQLite < 3.35
doesn't support DROP COLUMN; Alembic's ``batch_alter_table`` handles
this transparently via the rebuild-table pattern.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table lets SQLite handle the ADD COLUMN with default
    # (older versions can't ALTER … ADD COLUMN with NOT NULL otherwise).
    # On Postgres / MySQL it falls back to native ALTER COLUMN.
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "input_tokens",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "output_tokens",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "cost_usd",
                sa.Float(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "turn_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
    op.create_index(
        "ix_sessions_completed_at", "sessions", ["ended_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_completed_at", table_name="sessions")
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("turn_count")
        batch_op.drop_column("cost_usd")
        batch_op.drop_column("output_tokens")
        batch_op.drop_column("input_tokens")
