"""session_version_paused_at_hitl_version.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-23

Plan 7 Task 6 / D7.5 — adds optimistic locking version columns to
``sessions`` and ``hitl_requests``, plus a ``paused_at`` timestamp on
``sessions`` for the pause-timeout watchdog (D6 pause/resume + Plan 7
optimistic concurrency).

``version`` defaults to 0 on existing rows via ``server_default``; new
rows get 0 from the schema-side default. The column is bumped explicitly
on every state transition by ``SqlAlchemyStore.update_session_status`` /
``resolve_hitl_request`` once Plan 7 Task 8 lands. Until then the column
sits dormant — adding the storage now lets Task 7 (events table, 0004)
and Task 8 (optimistic locking) land independently without re-migrating.

Compatibility:
  * **SQLite + Postgres** — uses ``server_default='0'`` so existing
    rows are populated automatically by the ALTER without a separate
    UPDATE pass. Both dialects accept the literal '0' as a default for
    INTEGER columns.
  * ``nullable=False`` is safe AFTER the server_default fills existing
    rows.
  * ``paused_at`` is nullable (existing sessions never paused).

Downgrade drops the new columns. SQLite < 3.35 doesn't support DROP
COLUMN; Alembic's ``batch_alter_table`` handles the rebuild-table
fallback transparently.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "version",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "paused_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
    with op.batch_alter_table("hitl_requests") as batch_op:
        batch_op.add_column(
            sa.Column(
                "version",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("hitl_requests") as batch_op:
        batch_op.drop_column("version")
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("paused_at")
        batch_op.drop_column("version")
