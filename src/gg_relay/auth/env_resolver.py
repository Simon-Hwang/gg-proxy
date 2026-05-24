"""EnvKeyResolver — boot-time sync of env keys into the DB.

Plan 8 D8.29 step 2 / Task 22. Runtime key resolution lives in
:class:`gg_relay.auth.db_resolver.DBKeyResolver`; this resolver only
exists to bootstrap the existing ``RELAY_API_KEYS_RAW`` env keys
into the new ``api_keys`` table at lifespan startup so an existing
deployment migrates without operator intervention.

Idempotent. If a row with the same ``label`` already exists, the
sync skips the insert (DB takes precedence — the operator may have
rotated the key through the admin endpoint after the initial
bootstrap, and re-importing the env value would undo their change).
A loud warning is emitted when the env key for an existing label
differs from the stored hash so operators notice the drift.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from gg_relay.auth.store import ApiKeyStore, hash_key

logger = logging.getLogger("gg_relay.auth.env_resolver")


class EnvKeyResolver:
    """Bootstrap-only resolver: writes env keys to DB on startup.

    Not a :class:`gg_relay.auth.protocol.KeyResolver` implementation
    by itself — request-time resolution is the DBKeyResolver's job.
    The name keeps parity with the plan brief; the only public
    method is :meth:`sync_to_db`.

    Construction args:

      * ``env_keys_with_labels`` — ``{raw_key: label}`` parsed from
        ``RELAY_API_KEYS_RAW`` (Plan 7 D7.26).
      * ``role_mapping``         — ``{label: role}`` parsed from
        ``RELAY_ROLE_MAPPING_RAW`` (Plan 8 D8.22). Looked up
        per-label when minting the row's role; missing labels
        fall back to ``default_role`` so a fresh deploy isn't
        forced to set the env var before the bootstrap can run.
      * ``key_store``            — :class:`ApiKeyStore` instance.
      * ``default_role``         — fallback when no mapping is
        available. ``"submitter"`` by default — a sensible
        middle-ground that lets the key submit work but not
        administer roles.
    """

    def __init__(
        self,
        *,
        env_keys_with_labels: Mapping[str, str],
        role_mapping: Mapping[str, str],
        key_store: ApiKeyStore,
        default_role: str = "submitter",
    ) -> None:
        self._keys = dict(env_keys_with_labels)
        self._role_mapping = dict(role_mapping)
        self._store = key_store
        self._default_role = default_role

    async def sync_to_db(self) -> dict[str, Any]:
        """Iterate env keys, insert any that are missing into DB.

        Returns a summary ``{"created": int, "skipped": int}`` for
        the lifespan logger so operators can see whether the
        bootstrap landed any rows or just no-op'd against an
        already-populated table.

        DB precedence rule: if the label exists but the stored
        ``key_hash`` differs from ``hash_key(raw_key)``, log a
        warning and skip (do NOT overwrite). Operators who really
        want to re-import the env value must revoke the existing
        row through the admin endpoint first.
        """
        created = 0
        skipped = 0
        for raw_key, label in self._keys.items():
            existing = await self._store.get_by_label(label)
            if existing is not None:
                if existing["key_hash"] != hash_key(raw_key):
                    logger.warning(
                        "env api_key for label %r differs from DB row; "
                        "DB takes precedence (revoke the DB row before "
                        "re-importing the env value)",
                        label,
                    )
                skipped += 1
                continue
            role = self._role_mapping.get(label, self._default_role)
            await self._store.create(
                label=label,
                raw_key=raw_key,
                role=role,
                created_by_label="env_bootstrap",
                notes="Auto-imported from RELAY_API_KEYS_RAW env",
            )
            created += 1
        return {"created": created, "skipped": skipped}
