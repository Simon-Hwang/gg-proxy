"""events_seq_backfill — Plan 9 v0.9.0-rc D9.9 phase 2 (data + index).

Backfills the ``events.seq`` column added by 0012a, flips it to
NOT NULL, and creates the unique index. This migration is **operator-
triggered** (NOT auto-run by ``gg-relay migrate`` to head) because:

1. **Rolling-deploy window.** v0.8.x pods may still be writing
   ``NULL`` to the seq column during the rollout to v0.9.0-rc.
   Backfilling before the window closes would race those writes and
   produce a UNIQUE constraint violation on the index create. The
   ``gg-relay migrate --to 0012b`` runbook step instructs the
   operator to run this only after they've verified no NULL writes
   are happening for at least 24 hours (the v0.9.1 D9.5 expansion
   surfaces ``gg_relay_null_seq_writes_total`` for this check).

2. **``CREATE INDEX CONCURRENTLY`` on Postgres** requires escaping
   Alembic's default transaction wrapper via
   ``op.get_context().autocommit_block()`` so the operator can
   monitor the long-running index build without holding an
   ACCESS EXCLUSIVE lock. SQLite has no CONCURRENTLY equivalent
   (single-writer model) so the index there is built inside the
   normal transaction — fine because SQLite deployments are dev /
   small-team scale where blocking briefly is acceptable.

Postgres backfill sequence:
  1. ``UPDATE events SET seq = nextval('events_seq_seq') WHERE seq IS NULL``
  2. ``SELECT setval('events_seq_seq', (SELECT MAX(seq) FROM events))``
     — critical! Without this, the next ``nextval()`` after the
     backfill would reuse already-assigned numbers and break the
     unique index. Reviewer G Round 3 BLOCKER 4 caught the
     original draft that omitted this step.
  3. ``ALTER COLUMN seq SET NOT NULL``
  4. ``CREATE UNIQUE INDEX CONCURRENTLY ix_events_seq ON events (seq)``

SQLite backfill sequence:
  1. ``UPDATE events SET seq = COALESCE((SELECT MAX(seq) FROM events),0) + rowid
     WHERE seq IS NULL``
     — uses rowid for the per-row offset since SQLite has no
     server-side sequence object. Guaranteed unique because rowid
     is unique within the table.
  2. ``batch_alter_table`` to flip ``seq`` NOT NULL.
  3. ``op.create_index`` (unique) — non-CONCURRENTLY on SQLite.

Revision ID: 0012b
Revises: 0012a
Create Date: 2026-05-24
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0012b"
down_revision: str | None = "0012a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == "postgresql":
        # Phase 1 — backfill any NULL seq via the sequence.
        op.execute(
            "UPDATE events SET seq = nextval('events_seq_seq') "
            "WHERE seq IS NULL"
        )
        # Phase 2 — re-align the sequence so the next nextval() does
        # not collide with already-assigned backfill values. The
        # COALESCE handles an empty table (sequence stays at 1).
        op.execute(
            "SELECT setval('events_seq_seq', "
            "(SELECT COALESCE(MAX(seq), 1) FROM events))"
        )
        # Phase 3 — flip the column to NOT NULL now that no rows
        # carry NULL.
        op.alter_column("events", "seq", nullable=False)
        # Phase 4 — build the unique index outside the implicit
        # Alembic transaction so writers are not blocked.
        # ``autocommit_block`` is the documented Alembic API for
        # CONCURRENTLY (it switches the bind to autocommit for the
        # duration of the block).
        with op.get_context().autocommit_block():
            op.execute(
                "CREATE UNIQUE INDEX CONCURRENTLY ix_events_seq "
                "ON events (seq)"
            )
    else:
        # SQLite (and any other dialect that doesn't support
        # sequences / CONCURRENTLY): single-row UPDATE using rowid
        # for per-row offsets so each backfilled row gets a unique
        # seq value.
        op.execute(
            text(
                "UPDATE events SET seq = "
                "COALESCE((SELECT MAX(seq) FROM events), 0) + rowid "
                "WHERE seq IS NULL"
            )
        )
        with op.batch_alter_table("events") as batch_op:
            batch_op.alter_column("seq", nullable=False)
        op.create_index("ix_events_seq", "events", ["seq"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_events_seq")
        op.alter_column("events", "seq", nullable=True)
    else:
        op.drop_index("ix_events_seq", table_name="events")
        with op.batch_alter_table("events") as batch_op:
            batch_op.alter_column("seq", nullable=True)
