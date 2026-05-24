"""Plan 9 D9.10 — DashboardKeyStore (DB-stored dashboard internal keys).

Backs the multi-worker fix for the per-pod ``secrets.token_urlsafe``
derivation that today (v0.8.x) breaks cookie-signed cross-pod
requests. The lifespan now calls
:meth:`DashboardKeyStore.get_or_create` per dashboard user so every
pod in the cluster shares the same raw_key for that username.

Why DB-stored (not Redis)
~~~~~~~~~~~~~~~~~~~~~~~~~

We already have an authenticated Postgres connection; using it for
the canonical key store keeps the lifespan dependency surface
small. Redis carries the *invalidation broadcast* (Plan 9
:class:`gg_relay.cluster.key_invalidate.KeyInvalidateSubscriber`)
because the broadcast is naturally pub/sub-shaped — but the
authoritative copy lives in DB so a Redis flush doesn't lose keys.

Plaintext trade-off
~~~~~~~~~~~~~~~~~~~

``raw_key`` is plaintext in this table. Documented in
``schema.py`` and the Santa Round 3 Reviewer G blocker resolution:
operators MUST restrict GRANT on this table to the gg-relay app
role, and audit roles MUST NOT read ``raw_key``. Plan 11+ may
upgrade to bcrypt-hashed with the plaintext kept only in
lifespan-process memory.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.store.schema import dashboard_internal_keys

# 43 chars = base64-url(32 bytes) without padding. Matches the
# CHECK constraint length on the schema column.
_KEY_BYTES = 32


def _generate_raw_key() -> str:
    """Generate a 43-char base64-url key. ``secrets.token_urlsafe``
    produces ``⌈4*n/3⌉ - padding`` chars; 32 bytes → 43 chars."""
    return secrets.token_urlsafe(_KEY_BYTES)[:43]


class DashboardKeyStore:
    """Async CRUD for ``dashboard_internal_keys`` table."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get(self, username: str) -> str | None:
        """Return the stored ``raw_key`` for ``username`` or ``None``."""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(dashboard_internal_keys.c.raw_key).where(
                    dashboard_internal_keys.c.username == username
                )
            )
            row = result.first()
        return row[0] if row is not None else None

    async def list_all(self) -> dict[str, str]:
        """Return the full ``{username: raw_key}`` snapshot.

        Used by the lifespan to seed ``app.state.dashboard_internal_keys``
        and by :class:`KeyInvalidateSubscriber` to refresh after a
        rotation broadcast.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(
                    dashboard_internal_keys.c.username,
                    dashboard_internal_keys.c.raw_key,
                )
            )
            rows = result.all()
        return {row[0]: row[1] for row in rows}

    async def get_or_create(self, username: str) -> str:
        """Atomic "fetch existing, else insert new" + return raw_key.

        Uses dialect-specific ``ON CONFLICT DO NOTHING`` so two
        workers racing on the same username can't double-insert
        (UNIQUE PK on username triggers the rollback). The follow-up
        SELECT then returns whichever raw_key won the race.
        """
        new_key = _generate_raw_key()
        now = datetime.now(UTC)
        dialect = self._engine.dialect.name
        async with self._engine.begin() as conn:
            if dialect == "postgresql":
                stmt: Any = pg_insert(dashboard_internal_keys).values(
                    username=username,
                    raw_key=new_key,
                    created_at=now,
                    rotated_at=now,
                )
                stmt = stmt.on_conflict_do_nothing(index_elements=["username"])
            else:
                stmt = sqlite_insert(dashboard_internal_keys).values(
                    username=username,
                    raw_key=new_key,
                    created_at=now,
                    rotated_at=now,
                )
                stmt = stmt.on_conflict_do_nothing(index_elements=["username"])
            await conn.execute(stmt)
            # SELECT after — yields either the just-inserted row or the
            # row another worker beat us to.
            row = (
                await conn.execute(
                    select(dashboard_internal_keys.c.raw_key).where(
                        dashboard_internal_keys.c.username == username
                    )
                )
            ).first()
        assert row is not None, "race-safe insert must produce a row"
        return str(row[0])

    async def rotate(self, username: str) -> str:
        """Force-replace the stored key with a new random value.

        Returns the *new* raw_key. The caller (admin endpoint /
        CLI) is responsible for publishing the corresponding
        ``KeyInvalidated`` event so other pods reload their
        ``app.state.dashboard_internal_keys`` mapping.
        """
        new_key = _generate_raw_key()
        now = datetime.now(UTC)
        async with self._engine.begin() as conn:
            await conn.execute(
                dashboard_internal_keys.update()
                .where(dashboard_internal_keys.c.username == username)
                .values(raw_key=new_key, rotated_at=now)
            )
        return new_key

    async def delete(self, username: str) -> None:
        """Remove the row entirely. Used when an operator removes
        the user from ``dashboard_users`` config."""
        async with self._engine.begin() as conn:
            await conn.execute(
                dashboard_internal_keys.delete().where(
                    dashboard_internal_keys.c.username == username
                )
            )


__all__ = ["DashboardKeyStore"]
