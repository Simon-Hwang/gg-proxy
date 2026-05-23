"""add_events_table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-23

Plan 7 Task 7 / D7.17 — durable event store table backing the optional
disk-tier of ``AsyncEventBus``. Plan 7 Task 13 wires the
``DurableEventStore`` Protocol to write here; this migration only
provisions storage so the bus implementation can land independently.

Schema:
  * ``event_id``      — PK, UUID string (36 chars)
  * ``ts``            — timezone-aware datetime, indexed (replay range scan)
  * ``type``          — event type string (e.g. ``"session.lifecycle.started"``)
  * ``session_id``    — optional reference (indexed for per-session replay;
    NULL for non-session events). **No foreign key** — see below.
  * ``payload``       — JSON dict (the full ``RelayEvent`` body).
    ``sa.JSON()`` maps to TEXT on SQLite and JSONB on Postgres via the
    dialect-aware impl.
  * ``delivery_tier`` — ``"in_process"`` | ``"disk"`` | ``"redis"``
    (Plan 8 adds the Redis tier; Plan 7 only emits in_process | disk).

No foreign keys: the events table is append-only telemetry. A
``DELETE FROM sessions`` cascade would wipe audit history, which is
the opposite of what a durable bus needs. Operators prune events via
a TTL job (Plan 8) instead.

Indexes:
  * ``ix_events_ts``         — range-scan replay (e.g. "events since T").
  * ``ix_events_session_id`` — per-session replay.

Compatibility:
  * SQLite + Postgres — pure ``op.create_table``; no batch ALTER
    needed because the table is brand new.
  * Downgrade drops the table cleanly on both dialects.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("event_id", sa.String(length=36), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("delivery_tier", sa.String(length=10), nullable=False),
    )
    op.create_index("ix_events_ts", "events", ["ts"])
    op.create_index("ix_events_session_id", "events", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_events_session_id", table_name="events")
    op.drop_index("ix_events_ts", table_name="events")
    op.drop_table("events")
