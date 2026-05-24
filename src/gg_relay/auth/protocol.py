"""KeyResolver Protocol — Plan 8 Task 22 / D8.29.

This module defines the structural contract every key resolution
strategy must satisfy so :class:`gg_relay.api.middleware.api_key_auth.APIKeyAuthMiddleware`
can resolve raw ``X-API-Key`` headers without caring whether the
backing store is in-memory (Plan 7), the new DB-backed table
(Plan 8 Task 22), or a future composite (env-then-db, Redis-cached,
…).

Two surface methods:

  * :meth:`KeyResolver.resolve` — fast hot-path lookup. Called on
    every authenticated request; must be async to allow DB / Redis
    backends to await IO without blocking the event loop. Returns a
    :class:`ResolvedKey` on success or ``None`` if the key is
    invalid, revoked, or expired — the middleware turns ``None``
    into a 401 without leaking which of the three caused it (timing
    + body shape uniformity, see D7.15).

  * :meth:`KeyResolver.invalidate_cache` — admin-mutation hook so a
    POST that creates a new key or a DELETE that revokes one can
    drop any cached entry for the affected hash/label. Both
    arguments are keyword-only and optional; passing neither
    clears the entire cache (used during shutdown / test teardown).

:class:`ResolvedKey` is a frozen dataclass so a resolver may safely
share a single instance across threads / coroutines without worrying
about mutation. The fields mirror the columns the
:class:`gg_relay.api.middleware.api_key_auth.APIKeyAuthMiddleware`
writes to ``request.state``:

  * ``label``      — operator-visible identifier (audit log actor +
    owner attribution).
  * ``role``       — ``viewer`` / ``submitter`` / ``admin`` per
    Plan 8 D8.22. The resolver MAY honour
    ``cfg.role_override_mode="config"`` to remap the DB role
    through ``cfg.role_mapping`` for emergency lockdown scenarios
    (see :class:`gg_relay.auth.db_resolver.DBKeyResolver`).
  * ``expires_at`` — optional, surfaced for the dashboard "expiring
    soon" view; the middleware itself doesn't read it (the resolver
    has already short-circuited expired keys before returning).
  * ``notes``      — optional free-form metadata copied verbatim
    from the row; the middleware doesn't read it either, but tests
    and the dashboard list endpoint use it via the resolver's
    ``ResolvedKey`` return shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ResolvedKey:
    """Immutable result of a successful :meth:`KeyResolver.resolve`.

    Frozen so a resolver can cache the instance and hand the same
    object back to many concurrent requests without worrying about
    aliasing. The middleware copies ``label`` and ``role`` onto
    ``request.state`` so downstream consumers (require_role,
    owner attribution, audit fallback) don't need to import this
    dataclass.
    """

    label: str
    role: str
    expires_at: datetime | None = None
    notes: str | None = None


@runtime_checkable
class KeyResolver(Protocol):
    """Async-only structural contract for API key resolution.

    Plan 8 D8.29 — implementations:

      * :class:`gg_relay.auth.db_resolver.DBKeyResolver` — TTL cache
        over the ``api_keys`` table (the production path).
      * :class:`gg_relay.auth.env_resolver.EnvKeyResolver` — boot-time
        sync of ``RELAY_API_KEYS_RAW`` env keys into the DB; does
        not itself serve resolve() (that's the DBKeyResolver's job).
      * Future composite (env-then-db, Redis-cached) implementations
        plug into the same ``app.state.key_resolver`` slot.

    Test resolvers may implement this Protocol with bare classes
    (the ``@runtime_checkable`` decoration lets ``isinstance(obj,
    KeyResolver)`` work without inheritance, though tests typically
    just duck-type-set ``app.state.key_resolver``).
    """

    async def resolve(self, raw_key: str) -> ResolvedKey | None:
        """Return ``ResolvedKey`` if ``raw_key`` is valid + active +
        non-expired; ``None`` otherwise.

        Implementations MUST NOT distinguish between "unknown key",
        "revoked key", and "expired key" in the return value — the
        middleware collapses all three to a single 401 body to keep
        the constant-time + uniform-body posture (D7.15).
        """
        ...

    async def invalidate_cache(
        self,
        *,
        key_hash: str | None = None,
        label: str | None = None,
    ) -> None:
        """Drop cached state for the supplied key.

        Pass ``key_hash`` when the caller already knows the hash
        (the admin POST / DELETE endpoints do). Pass ``label`` to
        let the resolver look up the hash on its own (covers
        recently-revoked rows where the cache entry needs to die
        but the caller may not have the hash handy). Pass neither
        to clear everything (test teardown).
        """
        ...
