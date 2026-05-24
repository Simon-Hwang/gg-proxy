"""events_seq_column — Plan 9 v0.9.0-rc D9.9 phase 1 (DDL only).

Adds the ``events.seq`` column as a **nullable** BIGINT and provisions
the Postgres ``events_seq_seq`` sequence object. This migration is
intentionally split from the backfill / NOT NULL flip (0012b) for two
reasons:

1. **Rolling-deploy safety.** During a multi-pod upgrade window, v0.8.x
   pods that still write to the ``events`` table have no knowledge of
   the ``seq`` column. Keeping ``seq`` nullable means their INSERTs
   continue to succeed (DB writes ``NULL``) while v0.9.0-rc pods fill
   the column via the application layer (``nextval`` for Postgres,
   ``INSERT...SELECT COALESCE(MAX(seq),0)+1`` for SQLite).

2. **No application-level coupling at this revision.** 0012a is pure
   DDL and is safe for any 0.9.x pod. 0012b adds the NOT NULL +
   ``CREATE INDEX CONCURRENTLY`` ceremony and runs only AFTER the
   operator has confirmed no NULL ``seq`` writes are happening (see
   ``docs/cluster.md`` runbook & ``gg_relay_null_seq_writes_total``
   metric the v0.9.1 D9.5 expansion will surface).

Schema change:
  * ``ALTER TABLE events ADD COLUMN seq BIGINT NULL`` (both dialects)
  * Postgres only: ``CREATE SEQUENCE IF NOT EXISTS events_seq_seq``
    — the application layer uses ``nextval('events_seq_seq')`` to fill
    the column atomically per insert. ``START WITH 1`` (default) is
    fine because 0012b runs ``setval`` after backfill to align the
    sequence with ``MAX(seq)`` before the unique index is created.

Note re: ``CREATE INDEX CONCURRENTLY``:
  * NOT done here — that's 0012b's job, gated behind
    ``op.get_context().autocommit_block()`` because CONCURRENTLY
    cannot run inside Alembic's default transaction. Splitting the
    migrations keeps this revision a clean inside-transaction DDL.

Revision ID: 0012a
Revises: 0011
Create Date: 2026-05-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012a"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("seq", sa.BigInteger(), nullable=True),
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Sequence object used by the application layer's
        # ``insert_event`` path. IF NOT EXISTS guards against re-runs
        # on partially-migrated databases (rare but harmless).
        op.execute("CREATE SEQUENCE IF NOT EXISTS events_seq_seq")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP SEQUENCE IF EXISTS events_seq_seq")
        op.drop_column("events", "seq")
    else:
        # SQLite (3.26+) cannot ``ALTER TABLE DROP COLUMN`` until
        # 3.35; ``batch_alter_table`` rewrites the table with the
        # column removed which is portable across the older bundled
        # SQLite shipped with stock Python.
        with op.batch_alter_table("events") as batch_op:
            batch_op.drop_column("seq")
