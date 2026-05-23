"""add_parent_session_id.

Plan 8 Task 9 / D8.6 — track retry lineage between sessions.

When a user retries a failed session, :meth:`SessionManager.retry`
creates a NEW session row whose ``parent_session_id`` points at the
original. The dashboard renders the resulting (sid → child sids)
relation as a retry tree so operators can follow the breadcrumb
trail when a session was retried multiple times before completing.

Schema:
  * ``sessions.parent_session_id`` — ``String(36)``, nullable. NOT
    enforced as a foreign key: a parent session may legitimately
    have been archived or deleted by the retention job before the
    child finishes. Keeping the column free of FK constraints lets
    children survive parent deletion (the dashboard renders an
    "(archived)" placeholder when the parent row no longer exists).
  * ``ix_sessions_parent_session_id`` — equality index for the
    canonical "list every retry of session X" query that powers
    the retry-tree view.

Compatibility:
  * SQLite + Postgres — pure ``op.add_column`` via
    ``batch_alter_table`` so SQLite < 3.35 (which lacks ALTER COLUMN
    on a fully-typed column) takes the rebuild fallback path.
  * Existing rows get ``NULL`` for ``parent_session_id`` (no
    server_default); the column is nullable, so the existing data
    stays valid without a backfill.
  * Downgrade drops the index first, then the column.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(
            sa.Column("parent_session_id", sa.String(length=36), nullable=True)
        )
    op.create_index(
        "ix_sessions_parent_session_id", "sessions", ["parent_session_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sessions_parent_session_id", table_name="sessions"
    )
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("parent_session_id")
