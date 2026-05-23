"""session_collaboration_metadata.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-23

Plan 7 Task 6b / D7.26 — owner + description for single-team
multi-maintainer collaboration. ``owner`` is auto-attributed by the
API router from ``request.state.api_key_label`` (set by
``APIKeyAuthMiddleware``) so existing clients get attribution for
free without code changes — they keep sending the same X-API-Key
header and the relay derives a stable label from the new
``RELAY_API_KEYS_RAW`` ``key:label`` / ``label=key`` token format
(legacy bare keys auto-derive a ``key-<sha256[:8]>`` label).

Schema:
  * ``owner``       — ``String(64)``, nullable, **indexed** (Kanban /
    dashboard filter "show me alice's running sessions" is hot — the
    plan 8 multi-team router will reuse this same predicate).
  * ``description`` — ``String(512)``, nullable. Truncation +
    ``X-Description-Truncated: true`` response header live at the
    router layer; the store sees at most 512 chars.

Indexes:
  * ``ix_sessions_owner`` — equality scan for the owner filter.

Compatibility:
  * SQLite + Postgres — pure ``op.add_column`` via batch_alter_table
    (SQLite < 3.35 needs the rebuild fallback that batch supplies).
  * Both columns are nullable, so existing rows stay valid without a
    server_default backfill.
  * Downgrade drops the index first, then both columns.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(
            sa.Column("owner", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("description", sa.String(length=512), nullable=True)
        )
    op.create_index("ix_sessions_owner", "sessions", ["owner"])


def downgrade() -> None:
    op.drop_index("ix_sessions_owner", table_name="sessions")
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("description")
        batch_op.drop_column("owner")
