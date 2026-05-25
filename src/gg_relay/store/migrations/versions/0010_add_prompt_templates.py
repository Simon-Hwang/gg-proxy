"""add_prompt_templates.

Plan 8 Task 14 / D8.24 — reusable prompt templates for team submission.

Schema:
  * ``name``         — short label, 128 char cap. Indexed for the
    canonical "search my templates by name" lookup.
  * ``creator``      — ``request.state.api_key_label`` (or ``"anon"``
    fallback). 64 char cap. Indexed because the visibility predicate
    "show me MY templates" is the hot path.
  * ``prompt``       — template body. Stored as ``Text`` so paragraph-
    sized prompts fit without truncation; the router caps at 20k chars
    so a runaway template can't exhaust the audit metadata budget.
  * ``description``  — optional short note (500 chars). Surfaced in
    the list view so users can pick templates by purpose without
    expanding the body.
  * ``shared``       — boolean flag. ``True`` exposes the template to
    every authenticated submitter/admin in the list endpoint;
    ``False`` keeps it private to the creator (admin override
    available via ``include_others=True``).
  * ``tags``         — optional CSV string ("ci,deploy,onboarding") so
    the future tag-filter UI can split templates without a separate
    join table — the dataset is small enough that a per-row LIKE
    scan is cheap.
  * ``created_at`` / ``updated_at`` — UTC stamps. PATCH bumps
    ``updated_at`` so the dashboard can sort by "recently edited".

Constraints + indexes:
  * ``uq_prompt_templates_creator_name`` — unique
    ``(creator, name)`` pair. Same user cannot create two
    templates with the same name (409 ``template_name_conflict``
    on collision); two DIFFERENT users may both name a template
    ``"deploy-prod"`` because the namespace is scoped per-creator
    (the row's ``shared`` flag still controls cross-user
    visibility).
  * ``ix_prompt_templates_shared_name`` — composite ``(shared,
    name)`` powers the canonical "list ALL shared templates
    alphabetically" query that drives the team-wide dropdown.
  * ``ix_prompt_templates_name`` and ``ix_prompt_templates_creator``
    are created implicitly via each column's ``index=True`` — they
    serve the equality scans (name search, "my templates").

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=128), nullable=False, index=True),
        sa.Column(
            "creator", sa.String(length=64), nullable=False, index=True
        ),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column(
            "shared",
            sa.Boolean(),
            nullable=False,
            # ``sa.false()`` is dialect-aware: PostgreSQL emits
            # ``DEFAULT FALSE``, SQLite emits ``DEFAULT 0``. The
            # previous ``sa.text("0")`` round-tripped through SQLite
            # fine but crashed on Postgres with ``column "shared" is
            # of type boolean but default expression is of type
            # integer`` — a strict-typing difference between the two
            # dialects that hides until a real Postgres runs the
            # migration chain.
            server_default=sa.false(),
        ),
        sa.Column("tags", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "creator",
            "name",
            name="uq_prompt_templates_creator_name",
        ),
    )
    op.create_index(
        "ix_prompt_templates_shared_name",
        "prompt_templates",
        ["shared", "name"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_prompt_templates_shared_name", table_name="prompt_templates"
    )
    op.drop_table("prompt_templates")
