"""add_audit_log.

Plan 8 Task 5 / D8.4 — durable audit log for all sensitive mutations.

Schema: every mutation route writes ``(actor, action, target_type,
target_id, metadata)`` in the same transaction as the business update
(durable outbox pattern; v2.1 MAJOR 3); :class:`AuditFallbackMiddleware`
covers endpoints that forgot.

Indexes are tuned for the three hot dashboard / API queries:
  * ``ix_audit_log_ts``           — global newest-first time-series scan
  * ``ix_audit_log_actor_ts``     — "show me every action by alice"
  * ``ix_audit_log_target``       — composite ``(target_type, target_id)``
    for "session sid-xyz audit history" lookups

``request_id`` is non-indexed; it's a join key for cross-correlation with
the access log when the operator wants to trace a single request end to
end. Such joins are rare and small (a handful of rows per request), so
the extra index would cost more than it saves.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.String(length=128), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("request_id", sa.String(length=36), nullable=True),
    )
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"])
    op.create_index("ix_audit_log_actor_ts", "audit_log", ["actor", "ts"])
    op.create_index(
        "ix_audit_log_target", "audit_log", ["target_type", "target_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_target", table_name="audit_log")
    op.drop_index("ix_audit_log_actor_ts", table_name="audit_log")
    op.drop_index("ix_audit_log_ts", table_name="audit_log")
    op.drop_table("audit_log")
