"""add_api_keys_table.

Plan 8 Task 22 / D8.29 — DB-backed API key self-service.

Schema:
  * ``label``             — human-readable identifier (unique). 64 chars.
    The dashboard / audit log uses this as the operator-visible name
    for the key. Unique so an audit row can be traced back to one
    distinct human/integration without ambiguity.
  * ``key_hash``          — sha256 hex digest of the plaintext key
    (64 hex chars). The plaintext is **NEVER** stored: the create
    endpoint surfaces it once at mint time and never again. The
    hash is what :class:`DBKeyResolver` looks up at request time
    so a database leak does not expose the raw keys.
  * ``role``              — ``viewer`` / ``submitter`` / ``admin``.
    Resolved by :func:`gg_relay.api.dependencies.require_role`
    when the DB resolver is the active key source (Plan 8 v2.3
    BLOCKER 2 / ``role_override_mode='db'``).
  * ``created_at`` /
    ``created_by_label``  — audit trail of who created the key
    (``"env_bootstrap"`` for keys synced from
    ``RELAY_API_KEYS_RAW``, ``"lifespan_bootstrap"`` for the
    dashboard internal keys, the caller's label for admin POSTs).
  * ``expires_at``        — nullable. When set, :class:`DBKeyResolver`
    refuses the key past the timestamp (silent 401 — the cache
    drops the entry on the next lookup after expiry).
  * ``revoked_at``        — soft delete. Set by the admin DELETE
    endpoint; the resolver treats any non-NULL ``revoked_at`` as
    "key invalid" without losing the audit row.
  * ``last_used_at``      — throttled update (60s) so a hot key
    doesn't write per-request. Surfaced in the dashboard "stale
    keys" view so operators can spot keys nobody uses.
  * ``notes``             — optional free-form text (500 chars).
    Surfaced on the admin list so operators don't have to remember
    why they minted a key.

Constraints + indexes:
  * ``ux_api_keys_label``         — unique label. Creating a second
    key with an existing label raises :class:`gg_relay.core.exceptions.ApiKeyConflictError`
    → HTTP 409 ``api_key_label_conflict``.
  * ``ix_api_keys_key_hash``      — O(1) lookup on the request
    hot-path (:meth:`DBKeyResolver.resolve` hashes the inbound
    ``X-API-Key`` and does a single equality scan).
  * ``ix_api_keys_role_revoked``  — composite ``(role, revoked_at)``
    so :meth:`ApiKeyStore.count_active_admins` runs in one index
    seek (the last-admin guard in the DELETE endpoint).

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_label", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.UniqueConstraint("label", name="ux_api_keys_label"),
    )
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"])
    op.create_index(
        "ix_api_keys_role_revoked", "api_keys", ["role", "revoked_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_api_keys_role_revoked", table_name="api_keys")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")
