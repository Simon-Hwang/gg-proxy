"""user_credentials — Plan v3 §B per-user upstream credentials.

Adds the ``user_credentials`` table that backs the dashboard's
``/me/credentials`` and ``/admin/credentials`` self-service surface.
Each row carries one Fernet-encrypted environment variable (e.g.
``ANTHROPIC_API_KEY``) scoped to one ``user_label`` (which matches
the same identity ``api_keys.label`` uses, so cookie users and CLI
token users converge on the same row).

Why this is a NEW table instead of extending ``api_keys``:
  * api_keys is the authentication artefact (server-issued hash for
    bearer auth). It is "what proves you are alice".
  * user_credentials is the *upstream* secret (Anthropic / AWS /
    Vertex) that the relay-spawned SDK subprocess uses on alice's
    behalf. It is "what alice asks Anthropic to bill".
  * The two have orthogonal lifecycles — admins rotate api_keys,
    individuals rotate their own upstream creds.

Schema:
  * ``user_label`` mirrors ``api_keys.label`` (no FK — labels can
    legitimately exist in user_credentials before any api_keys row
    is minted, e.g. when an admin pre-provisions credentials for a
    new operator about to join).
  * ``env_name`` is constrained at the API layer to a hard-coded
    allowlist (``api/routers/user_credentials.py:ALLOWED_ENV_NAMES``)
    so a malicious caller cannot smuggle ``PATH`` / ``LD_PRELOAD``
    / ``PYTHONPATH`` into the SDK env.
  * ``value_encrypted`` is the Fernet ciphertext; the symmetric key
    comes from ``RELAY_CREDENTIALS_ENCRYPTION_KEY`` (Plan v3 §B.2).
  * ``key_fingerprint`` stores the first 16 hex chars of SHA-256 of
    the encryption key. Lets ``gg-relay list-bricked-credentials``
    identify rows encrypted with a now-stale key WITHOUT a
    decrypt-and-retry loop. Also lets the store skip-and-log rows
    that no longer match the current key (graceful degradation
    after a key change instead of poisoning every submit).
  * ``created_by_label`` is the actor of the most recent write. For
    self-service writes it equals ``user_label``; for admin
    overrides it equals the admin's label. The dashboard surfaces
    this so the user can tell which rows an admin touched.
  * UNIQUE(user_label, env_name) makes the API ``PUT`` semantics an
    UPSERT (one row per env var per user).

Compatibility:
  * SQLite — supports ``BLOB`` natively, ``DateTime(timezone=True)``
    via the SQLAlchemy dialect.
  * Postgres — same schema, ``LargeBinary`` → ``bytea``.

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-25
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_credentials",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_label", sa.String(length=64), nullable=False),
        sa.Column("env_name", sa.String(length=64), nullable=False),
        sa.Column("value_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("key_fingerprint", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("created_by_label", sa.String(length=64), nullable=False),
        sa.Column("notes", sa.String(length=512), nullable=True),
        sa.UniqueConstraint(
            "user_label",
            "env_name",
            name="uq_user_credentials_label_env",
        ),
    )
    op.create_index(
        "ix_user_credentials_user_label",
        "user_credentials",
        ["user_label"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_credentials_user_label",
        table_name="user_credentials",
    )
    op.drop_table("user_credentials")
