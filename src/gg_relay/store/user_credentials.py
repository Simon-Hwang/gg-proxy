"""Per-user upstream credentials store — Plan v3 §B.

Encrypts each row's ``value`` at rest with Fernet and exposes a tiny
async CRUD surface over the ``user_credentials`` table. The store is
the ONLY layer that touches plaintext values; everything above it
(manager merge, API routes, dashboard pages) sees metadata-only
projections.

Failure modes (graceful degradation, never poisons a submit):

* **Feature disabled** (``fernet=None``) — ``get_for_user`` returns
  ``{}``, writes raise :class:`UserCredentialsFeatureDisabled`. The
  manager merge gates on the store being non-None *and* ``actor_label``
  being set, so this collapses to a no-op submit-time path.
* **Row whose ``key_fingerprint`` doesn't match the current key** —
  log warning + skip the row in ``get_for_user``. The
  ``gg-relay list-bricked-credentials`` CLI surfaces the row so an
  operator can re-enter the value. Partial returns are intentional:
  a single bricked row should not block the user's working creds.
* **Fernet ``InvalidToken`` on decrypt** (tampered ciphertext, key
  mismatch despite a fingerprint match) — same skip-and-log path.
* **DB error** — propagates up; the manager's submit wraps the call
  in a try/except so a DB hiccup logs a warning + skips the merge
  rather than 5xx-ing the user's session.

This module imports SQLAlchemy + ``cryptography.fernet`` only; no
framework code lives here so the manager can keep its
framework-agnostic invariant (Plan v3 §0).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import and_, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.store.schema import user_credentials

logger = logging.getLogger(__name__)


class UserCredentialsFeatureDisabled(RuntimeError):
    """Raised when a write is attempted while the feature is disabled.

    Disabled means ``RELAY_CREDENTIALS_ENCRYPTION_KEY`` is unset OR
    ``RELAY_DISABLE_USER_CREDENTIALS=true``. The API routes catch this
    and return ``503 user_credentials_disabled`` so the client knows
    the rejection is configuration-level, not auth-level.
    """


def compute_key_fingerprint(key: str | bytes) -> str:
    """Stable 16-hex-char prefix of SHA-256(key).

    Used to tag every encrypted row so a future key rotation can:
      * surface "stale-key" rows via ``list_bricked`` (CLI helper),
      * skip stale-key rows in ``get_for_user`` (graceful degradation
        instead of poisoning every submit with InvalidToken).

    The fingerprint deliberately omits the trailing 48 bits of the
    digest — even if a row's fingerprint leaked, it would not
    materially help an attacker brute-force the symmetric key
    (Fernet keys are 256 bits).
    """
    if isinstance(key, str):
        key = key.encode("utf-8")
    return hashlib.sha256(key).hexdigest()[:16]


class UserCredentialsStore:
    """Async CRUD over ``user_credentials`` with Fernet at rest.

    One instance per process is the expected pattern (lifespan
    attaches it to ``app.state.user_credentials_store``). When
    ``fernet`` is ``None`` the feature is treated as disabled —
    reads return empty and writes raise.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        fernet: Fernet | None,
        key_fingerprint: str | None,
    ) -> None:
        self._engine = engine
        self._fernet = fernet
        self._key_fingerprint = key_fingerprint

    @property
    def enabled(self) -> bool:
        """Feature is live (encryption key present)."""
        return self._fernet is not None

    def _require_enabled(self) -> Fernet:
        if self._fernet is None:
            raise UserCredentialsFeatureDisabled(
                "user_credentials feature is disabled "
                "(RELAY_CREDENTIALS_ENCRYPTION_KEY not set "
                "or RELAY_DISABLE_USER_CREDENTIALS=true)"
            )
        return self._fernet

    # ── reads ──────────────────────────────────────────────────────────

    async def get_for_user(self, label: str) -> dict[str, str]:
        """Return ``{env_name: decrypted_value}`` for the user.

        Returns ``{}`` when:

        * the feature is disabled (``fernet=None``),
        * there are no rows for ``label``,
        * **a single bricked row** is encountered — that row is
          skipped and the rest of the user's good rows are still
          returned (partial-good semantics).

        Never raises; a DB-level error propagates and is meant to
        be caught by the caller (the manager merge logs + falls
        through with empty creds rather than blocking the submit).
        """
        if self._fernet is None or not label:
            return {}
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(
                    user_credentials.c.env_name,
                    user_credentials.c.value_encrypted,
                    user_credentials.c.key_fingerprint,
                ).where(user_credentials.c.user_label == label)
            )
            rows = result.mappings().all()

        out: dict[str, str] = {}
        for row in rows:
            env_name = row["env_name"]
            row_fp = row["key_fingerprint"]
            if (
                self._key_fingerprint is not None
                and row_fp != self._key_fingerprint
            ):
                logger.warning(
                    "user_credentials row bricked "
                    "(label=%s env=%s row_fp=%s current_fp=%s) — "
                    "skipping; run `gg-relay list-bricked-credentials` "
                    "to surface for re-entry",
                    label,
                    env_name,
                    row_fp,
                    self._key_fingerprint,
                )
                continue
            try:
                plaintext = self._fernet.decrypt(row["value_encrypted"])
            except InvalidToken:
                logger.warning(
                    "user_credentials row failed Fernet.decrypt "
                    "(label=%s env=%s row_fp=%s) — skipping",
                    label,
                    env_name,
                    row_fp,
                )
                continue
            out[env_name] = plaintext.decode("utf-8")
        return out

    async def list_for_user(self, label: str) -> list[dict[str, Any]]:
        """Metadata-only projection for the dashboard / API.

        Never returns plaintext — even admin paths render only
        ``env_name``, ``updated_at``, ``created_by_label``, etc.
        Use ``get_for_user`` (manager-only) when you need values.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(
                    user_credentials.c.id,
                    user_credentials.c.user_label,
                    user_credentials.c.env_name,
                    user_credentials.c.key_fingerprint,
                    user_credentials.c.created_at,
                    user_credentials.c.updated_at,
                    user_credentials.c.created_by_label,
                    user_credentials.c.notes,
                )
                .where(user_credentials.c.user_label == label)
                .order_by(user_credentials.c.env_name)
            )
            return [dict(row) for row in result.mappings().all()]

    async def list_all(self) -> list[dict[str, Any]]:
        """Admin view across every user. Metadata only."""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(
                    user_credentials.c.id,
                    user_credentials.c.user_label,
                    user_credentials.c.env_name,
                    user_credentials.c.key_fingerprint,
                    user_credentials.c.created_at,
                    user_credentials.c.updated_at,
                    user_credentials.c.created_by_label,
                    user_credentials.c.notes,
                ).order_by(
                    user_credentials.c.user_label,
                    user_credentials.c.env_name,
                )
            )
            return [dict(row) for row in result.mappings().all()]

    async def list_bricked(self) -> list[dict[str, Any]]:
        """Rows whose ``key_fingerprint`` doesn't match the current key.

        Powers ``gg-relay list-bricked-credentials`` and the admin
        dashboard's "bricked" tab. Metadata only; no decrypt
        attempted (the rows are bricked precisely because we can't
        decrypt them with the current key).
        """
        if self._key_fingerprint is None:
            # Feature disabled — by definition every row is "bricked"
            # since we can't decrypt any of them. Surface them all
            # so the operator can re-enter after enabling encryption.
            async with self._engine.connect() as conn:
                result = await conn.execute(
                    select(
                        user_credentials.c.id,
                        user_credentials.c.user_label,
                        user_credentials.c.env_name,
                        user_credentials.c.key_fingerprint,
                        user_credentials.c.updated_at,
                    ).order_by(
                        user_credentials.c.user_label,
                        user_credentials.c.env_name,
                    )
                )
                return [dict(row) for row in result.mappings().all()]
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(
                    user_credentials.c.id,
                    user_credentials.c.user_label,
                    user_credentials.c.env_name,
                    user_credentials.c.key_fingerprint,
                    user_credentials.c.updated_at,
                )
                .where(
                    user_credentials.c.key_fingerprint != self._key_fingerprint
                )
                .order_by(
                    user_credentials.c.user_label,
                    user_credentials.c.env_name,
                )
            )
            return [dict(row) for row in result.mappings().all()]

    # ── writes ─────────────────────────────────────────────────────────

    async def upsert(
        self,
        *,
        user_label: str,
        env_name: str,
        value: str,
        actor_label: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Encrypt + UPSERT one row; return metadata (no plaintext).

        UPSERT contract (Plan v3 B.3 docstring):

          * INSERT when ``(user_label, env_name)`` is new.
          * UPDATE on conflict, replacing ``value_encrypted``,
            ``key_fingerprint``, ``updated_at``, ``created_by_label``
            and ``notes``. ``created_at`` is preserved.
          * ``created_by_label`` INTENTIONALLY tracks the most-recent
            writer — for admin overrides this lets the dashboard show
            "last touched by admin X", which is exactly the audit
            story B.8.3.g pins.

        Dialect: we use the SQLAlchemy dialect-specific
        ``on_conflict_do_update`` for both Postgres and SQLite. Both
        dialects support the same syntax through SQLAlchemy 2.x.
        """
        fernet = self._require_enabled()
        if self._key_fingerprint is None:  # defensive — should never happen when fernet set
            raise UserCredentialsFeatureDisabled(
                "encryption key fingerprint unavailable"
            )
        now = datetime.now(UTC)
        ciphertext = fernet.encrypt(value.encode("utf-8"))

        dialect_name = self._engine.dialect.name
        if dialect_name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(user_credentials).values(
                user_label=user_label,
                env_name=env_name,
                value_encrypted=ciphertext,
                key_fingerprint=self._key_fingerprint,
                created_at=now,
                updated_at=now,
                created_by_label=actor_label,
                notes=notes,
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_user_credentials_label_env",
                set_={
                    "value_encrypted": stmt.excluded.value_encrypted,
                    "key_fingerprint": stmt.excluded.key_fingerprint,
                    "updated_at": stmt.excluded.updated_at,
                    "created_by_label": stmt.excluded.created_by_label,
                    "notes": stmt.excluded.notes,
                },
            )
        else:  # sqlite (and any other dialect that supports the same API)
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            stmt = sqlite_insert(user_credentials).values(
                user_label=user_label,
                env_name=env_name,
                value_encrypted=ciphertext,
                key_fingerprint=self._key_fingerprint,
                created_at=now,
                updated_at=now,
                created_by_label=actor_label,
                notes=notes,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["user_label", "env_name"],
                set_={
                    "value_encrypted": stmt.excluded.value_encrypted,
                    "key_fingerprint": stmt.excluded.key_fingerprint,
                    "updated_at": stmt.excluded.updated_at,
                    "created_by_label": stmt.excluded.created_by_label,
                    "notes": stmt.excluded.notes,
                },
            )

        try:
            async with self._engine.begin() as conn:
                await conn.execute(stmt)
                # Read back to get the canonical row (covers INSERT vs
                # UPDATE) — also lets the caller see ``created_at``
                # which is preserved on UPDATE.
                read = await conn.execute(
                    select(
                        user_credentials.c.id,
                        user_credentials.c.user_label,
                        user_credentials.c.env_name,
                        user_credentials.c.key_fingerprint,
                        user_credentials.c.created_at,
                        user_credentials.c.updated_at,
                        user_credentials.c.created_by_label,
                        user_credentials.c.notes,
                    ).where(
                        and_(
                            user_credentials.c.user_label == user_label,
                            user_credentials.c.env_name == env_name,
                        )
                    )
                )
                row = read.mappings().first()
        except IntegrityError:  # pragma: no cover - upsert should not race
            raise

        return dict(row) if row is not None else {}

    async def delete(self, *, user_label: str, env_name: str) -> bool:
        """Idempotent delete. Returns True iff a row was removed."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(user_credentials).where(
                    and_(
                        user_credentials.c.user_label == user_label,
                        user_credentials.c.env_name == env_name,
                    )
                )
            )
            return (result.rowcount or 0) > 0


def build_fernet_from_key(
    raw_key: str | None,
) -> tuple[Fernet | None, str | None]:
    """Resolve operator-supplied ``RELAY_CREDENTIALS_ENCRYPTION_KEY``.

    Returns ``(fernet, fingerprint)`` when ``raw_key`` is set and
    valid, else ``(None, None)``. A non-empty but invalid key
    (wrong length / wrong base64) raises so the operator notices
    immediately at startup — silently disabling on a typo would be
    a foot-gun.
    """
    if not raw_key:
        return None, None
    # Fernet validates length + alphabet in __init__; let it raise.
    fernet = Fernet(raw_key.encode("utf-8") if isinstance(raw_key, str) else raw_key)
    fingerprint = compute_key_fingerprint(raw_key)
    return fernet, fingerprint
