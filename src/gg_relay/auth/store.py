"""ApiKeyStore — CRUD facade over the ``api_keys`` table.

Plan 8 Task 22 / D8.29. The store is the only place that touches
:data:`gg_relay.store.schema.api_keys` directly; the resolver, the
admin router, and the dashboard all go through this thin facade so a
future Postgres-specific feature (e.g. partial unique index on
``revoked_at IS NULL``) only has to update one file.

Plaintext keys are **never** stored. :func:`hash_key` is the
canonical sha256 hex digest helper used by:

  * :meth:`ApiKeyStore.create` — at insert time.
  * :class:`gg_relay.auth.db_resolver.DBKeyResolver` — at request
    time, to look up the matching row by ``key_hash``.
  * :class:`gg_relay.api.middleware.api_key_auth.APIKeyAuthMiddleware`
    — to populate ``request.state.api_key_hash`` for the audit
    fallback middleware.

All CRUD methods accept an optional ``conn=...`` kwarg so callers
that already opened a transaction (the durable-outbox pattern from
v2.1 MAJOR 3) can write the row inside the same transaction as
their business mutation. Without ``conn`` the methods open their
own short-lived transaction.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.core.exceptions import ApiKeyConflictError
from gg_relay.store.schema import api_keys

logger = logging.getLogger("gg_relay.auth.store")


def hash_key(plaintext: str) -> str:
    """Compute sha256 hex digest of ``plaintext``.

    Used by both the writer (``ApiKeyStore.create``) and the reader
    (``DBKeyResolver.resolve``) so the hash function lives in one
    place. The 64-char hex shape fits the ``key_hash`` column's
    ``String(64)``.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class ApiKeyStore:
    """Async CRUD layer over the ``api_keys`` table.

    Construction is cheap; one instance per process is the expected
    pattern (lifespan attaches it to ``app.state.api_key_store``).
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(
        self,
        *,
        label: str,
        raw_key: str,
        role: str,
        created_by_label: str | None = None,
        expires_at: datetime | None = None,
        notes: str | None = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        """Insert one ``api_keys`` row; return the persisted dict.

        Raises :class:`ApiKeyConflictError` when the unique
        ``ux_api_keys_label`` index rejects a duplicate label. The
        return dict mirrors the columns (id / label / key_hash /
        role / created_at / created_by_label / expires_at /
        revoked_at / notes); ``revoked_at`` is always ``None`` on
        a freshly-inserted row.
        """
        kh = hash_key(raw_key)
        now = datetime.now(UTC)
        stmt = api_keys.insert().values(
            label=label,
            key_hash=kh,
            role=role,
            created_at=now,
            created_by_label=created_by_label,
            expires_at=expires_at,
            notes=notes,
        )
        try:
            if conn is not None:
                result = await conn.execute(stmt)
            else:
                async with self._engine.begin() as new_conn:
                    result = await new_conn.execute(stmt)
        except IntegrityError as exc:
            raise ApiKeyConflictError(
                f"api_key label {label!r} already exists"
            ) from exc
        new_id = (
            result.inserted_primary_key[0]
            if result.inserted_primary_key
            else None
        )
        return {
            "id": new_id,
            "label": label,
            "key_hash": kh,
            "role": role,
            "created_at": now,
            "created_by_label": created_by_label,
            "expires_at": expires_at,
            "revoked_at": None,
            "last_used_at": None,
            "notes": notes,
        }

    async def get_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        """Return the row matching ``key_hash`` or ``None``.

        Uses the ``ix_api_keys_key_hash`` index for an O(1) seek.
        Does NOT filter on ``revoked_at`` / ``expires_at`` here —
        the resolver enforces both (so it can negative-cache a
        revoked key just like an unknown one).
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                api_keys.select().where(api_keys.c.key_hash == key_hash)
            )
            row = result.mappings().first()
            return dict(row) if row is not None else None

    async def get_by_label(self, label: str) -> dict[str, Any] | None:
        """Return the row matching ``label`` or ``None``."""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                api_keys.select().where(api_keys.c.label == label)
            )
            row = result.mappings().first()
            return dict(row) if row is not None else None

    async def list(
        self,
        *,
        include_revoked: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return rows newest-first.

        ``include_revoked=False`` (default) filters out soft-deleted
        rows so the admin list page only shows actionable keys.
        Operators wanting an audit history pass ``True`` to surface
        the revoked rows too.
        """
        stmt = select(api_keys)
        if not include_revoked:
            stmt = stmt.where(api_keys.c.revoked_at.is_(None))
        stmt = stmt.order_by(api_keys.c.created_at.desc()).limit(limit)
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            return [dict(r) for r in result.mappings()]

    async def revoke(self, *, label: str, conn: Any = None) -> bool:
        """Mark the row's ``revoked_at = now``. Return ``True`` if
        the UPDATE matched a row that was previously active.

        Idempotent: revoking an already-revoked label returns
        ``False`` so the admin endpoint can collapse double-clicks
        to a single 404 without crashing.
        """
        stmt = (
            api_keys.update()
            .where(api_keys.c.label == label)
            .where(api_keys.c.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        if conn is not None:
            result = await conn.execute(stmt)
            return (result.rowcount or 0) > 0
        async with self._engine.begin() as new_conn:
            result = await new_conn.execute(stmt)
            return (result.rowcount or 0) > 0

    async def touch_last_used(self, *, key_hash: str) -> None:
        """Update ``last_used_at = now`` for the matching row.

        Called fire-and-forget from :class:`DBKeyResolver` with a
        60s throttle so a hot key doesn't write per-request.
        Silently swallows the case where the row has been revoked
        between cache-hit and the touch landing (the UPDATE just
        matches zero rows).
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                api_keys.update()
                .where(api_keys.c.key_hash == key_hash)
                .values(last_used_at=datetime.now(UTC))
            )

    async def count_active_admins(self) -> int:
        """Return the number of admin rows that are not revoked.

        Powers the last-admin guard in the admin DELETE endpoint:
        a revoke that would drop the count below 1 is refused.
        Uses the composite ``ix_api_keys_role_revoked`` index for
        a single seek.
        """
        stmt = (
            select(func.count())
            .select_from(api_keys)
            .where(api_keys.c.role == "admin")
            .where(api_keys.c.revoked_at.is_(None))
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            return int(result.scalar() or 0)
