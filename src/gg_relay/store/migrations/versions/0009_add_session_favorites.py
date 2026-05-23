"""add_session_favorites.

Plan 8 Task 13 / D8.21 — per-user session favorites for fast lookup.

Schema:
  * ``session_id``  — FK ``sessions.id`` with ``ON DELETE CASCADE`` so
    starring history is purged together with the session. The
    moderation trail still lives in ``audit_log`` (the star /
    unstar actions write ``session_star`` / ``session_unstar``
    rows there).
  * ``user_label``  — caller label string; same convention as
    ``audit_log.actor`` and ``session_comments.author``
    (``request.state.api_key_label`` or ``"anon"`` fallback).
    64 char cap.
  * ``created_at``  — UTC stamp of the star action. Sorted DESC by
    the dashboard "My Favorites" view so the most-recently starred
    session bubbles to the top.

Constraints + indexes:
  * ``uq_session_favorites_session_user`` — unique
    ``(session_id, user_label)`` pair. Star + un-star are
    idempotent: a second star surfaces as
    :class:`sqlalchemy.exc.IntegrityError` in the repository layer
    and is collapsed to ``added=False`` (no audit pollution).
  * ``ix_session_favorites_user_created`` — composite
    ``(user_label, created_at)`` powers the canonical "list MY
    favorites, newest first" query in one index seek.
  * ``ix_session_favorites_session_id`` is created implicitly via
    the column's ``index=True`` so the FK-locality scan stays cheap
    (e.g. when un-starring a session via the cascade path).
  * ``ix_session_favorites_user_label`` is created implicitly via
    the column's ``index=True`` — equality scans by the bare
    ``user_label`` predicate (without the ``created_at`` order)
    still hit an index, which the composite alone would only serve
    via a leading-column lookup.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_favorites",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_label", sa.String(length=64), nullable=False, index=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "session_id",
            "user_label",
            name="uq_session_favorites_session_user",
        ),
    )
    op.create_index(
        "ix_session_favorites_user_created",
        "session_favorites",
        ["user_label", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_favorites_user_created",
        table_name="session_favorites",
    )
    op.drop_table("session_favorites")
