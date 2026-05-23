"""add_session_comments.

Plan 8 Task 7 / D8.5 — comments on sessions for async collaboration.

Schema:
  * ``session_id``    — FK ``sessions.id`` with ``ON DELETE CASCADE`` so
    deleting a session purges its discussion (audit trail still lives
    in ``audit_log``).
  * ``author``        — label string; same actor convention as
    ``audit_log.actor`` (``request.state.api_key_label`` or
    ``"anon"`` fallback). 64 char cap.
  * ``body_markdown`` — raw markdown input as the user typed it. Kept
    so an edit pass can re-render under future sanitizer rules
    without losing the original intent.
  * ``body_html``     — sanitized HTML produced by
    :func:`gg_relay.comments.sanitizer.render_safe`
    (``markdown_it`` → ``bleach.clean``). Stored alongside the
    markdown so the dashboard renders without re-running the
    sanitizer per page-view.
  * ``created_at`` / ``updated_at`` — append-then-update timestamps;
    ``updated_at`` bumps on every PATCH so the UI can surface
    "edited" badges.
  * ``deleted_at``    — soft-delete tombstone. ``NULL`` for live rows;
    a non-null value hides the comment from the default list query
    while preserving the original content for moderation review.

Indexes:
  * ``ix_session_comments_session_created`` — composite
    ``(session_id, created_at)`` powers the canonical "list comments
    for a session, chronological order" query in one index seek.
  * ``ix_session_comments_session_id`` is created implicitly by the
    column's ``index=True`` (mirrors the FK locality benefit).
  * ``ix_session_comments_author``         — equality scan for the
    "my comments" lookup planned for Task 8's dashboard UI.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_comments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("author", sa.String(length=64), nullable=False, index=True),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_session_comments_session_created",
        "session_comments",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_comments_session_created", table_name="session_comments"
    )
    op.drop_table("session_comments")
