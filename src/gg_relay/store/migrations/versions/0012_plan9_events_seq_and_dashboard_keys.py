"""plan9_events_seq_and_dashboard_keys.

Plan 9 D9.9 + D9.10 — pre-production single-step migration that
provisions everything Plan 9 needs in one shot:

1. ``events.seq`` BIGINT column (NOT NULL + UNIQUE INDEX) backing
   the SSE ``Last-Event-ID: <seq>:<event_id>`` cursor.
2. Postgres ``events_seq_seq`` sequence object used by the
   application-layer ``persist`` (SQLite uses MAX+1 instead).
3. ``dashboard_internal_keys`` table for Plan 9 D9.10 multi-pod
   shared cookie keys.

This migration is **single-step** because gg-relay is pre-production
(no rolling-deploy compat window required). The v1.4 LOCKED design
that split this into 0012a (nullable add) + 0012b (operator-
triggered backfill + NOT NULL + CONCURRENTLY) is no longer needed
and was removed at v0.9.0 simplification.

Schema details:

* ``events.seq``    — NOT NULL BIGINT, UNIQUE indexed. Application
  layer writes via ``nextval('events_seq_seq')`` (Postgres) or
  ``COALESCE(MAX(seq), 0) + 1`` (SQLite) inside ``engine.begin()``.
* ``ix_events_seq`` — UNIQUE index on ``events.seq``. Both dialects
  build inline (no CONCURRENTLY — empty table at migration time
  since pre-production).
* ``dashboard_internal_keys`` — see schema docstring in
  ``schema.py``. Plaintext storage trade-off documented there.

Compatibility:
  * SQLite — supports BIGINT NOT NULL + INDEX inline (no batch).
  * Postgres — sequence + nextval, no special handling needed.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    # ── 1. events.seq column ────────────────────────────────────────
    # Add as nullable first, populate any existing rows, then flip
    # NOT NULL + UNIQUE INDEX. Pre-production guarantees the events
    # table is empty in practice, but the batch is defensive.
    op.add_column(
        "events",
        sa.Column("seq", sa.BigInteger(), nullable=True),
    )
    if dialect_name == "postgresql":
        op.execute("CREATE SEQUENCE IF NOT EXISTS events_seq_seq")
        op.execute(
            "UPDATE events SET seq = nextval('events_seq_seq') "
            "WHERE seq IS NULL"
        )
        op.execute(
            "SELECT setval('events_seq_seq', "
            "GREATEST((SELECT COALESCE(MAX(seq), 0) FROM events), 1))"
        )
        op.alter_column("events", "seq", nullable=False)
    else:
        # SQLite + every other dialect — backfill via rowid then flip
        # via batch_alter_table (cross-dialect safe).
        op.execute(
            "UPDATE events SET seq = "
            "COALESCE((SELECT MAX(seq) FROM events), 0) + rowid "
            "WHERE seq IS NULL"
        )
        with op.batch_alter_table("events") as batch_op:
            batch_op.alter_column("seq", nullable=False)
    op.create_index("ix_events_seq", "events", ["seq"], unique=True)

    # ── 2. dashboard_internal_keys table ────────────────────────────
    op.create_table(
        "dashboard_internal_keys",
        sa.Column("username", sa.String(length=64), primary_key=True),
        sa.Column("raw_key", sa.String(length=43), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "rotated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(raw_key) = 43",
            name="ck_dashboard_internal_keys_raw_key_length",
        ),
    )


def downgrade() -> None:
    op.drop_table("dashboard_internal_keys")
    op.drop_index("ix_events_seq", table_name="events")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP SEQUENCE IF EXISTS events_seq_seq")
        op.drop_column("events", "seq")
    else:
        with op.batch_alter_table("events") as batch_op:
            batch_op.drop_column("seq")
