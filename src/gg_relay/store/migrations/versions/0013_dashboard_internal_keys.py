"""dashboard_internal_keys — Plan 9 v0.9.0-rc D9.9 phase 3 (schema only).

Provisions the ``dashboard_internal_keys`` table that Plan 9.1 D9.10
will populate to fix the multi-worker dashboard cookie problem:
v0.8.x derives the per-username internal API key via
:func:`secrets.token_urlsafe(32)` at *each* pod startup. In a
multi-worker deployment that means worker A's cookie session is
signed with one key while worker B's is signed with another, and any
cross-worker cookie request gets 401'd silently.

v0.9.1 D9.10 will read this table in the lifespan via
``ApiKeyStore.get_or_create_dashboard_internal_key(username)`` so
**every pod in the cluster shares the same raw_key for a given
username**. Cookie-signed requests then validate against any pod.

v0.9.0-rc ships the table empty. The existing
:func:`_derive_dashboard_internal_keys` in ``api/main.py`` continues
to ``secrets.token_urlsafe`` per-pod for single-worker mode (where
this is fine — only one pod ever runs). v0.9.1 D9.10 will switch the
derivation to ``ApiKeyStore`` calls.

Schema:
  * ``username``    — PK. Matches ``api_keys.label`` namespace
    convention (``dashboard-<username>``) but holds the bare name.
  * ``raw_key``     — 43-byte plaintext (``secrets.token_urlsafe(32)``
    output length). CHECK constraint enforces length; the table is
    deliberately access-restricted at the application layer (only
    the lifespan reads it) and operators should GRANT read access
    only to the gg-relay app role.
  * ``created_at``  — set on initial mint.
  * ``rotated_at``  — updated by future
    ``gg-relay rotate-dashboard-keys`` CLI (Plan 9.1 D9.12).

Security note (Santa Round 3 Reviewer G #5):
  Plaintext storage of dashboard internal keys is a regression vs
  v0.8.x's per-pod ephemeral keys: any DB read (backup leak, SQL
  injection, audit role) now exposes a working key. Mitigations:
  * Plan 9.1 D9.10 documents the threat-model trade-off explicitly.
  * Plan 11+ may upgrade to bcrypt-hashed raw_key with the plaintext
    held only in the lifespan's process memory.
  * Operators are advised to restrict ``GRANT`` on this table to the
    gg-relay app role (read+write) and exclude it from audit-role
    read access.

Revision ID: 0013
Revises: 0012b
Create Date: 2026-05-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
