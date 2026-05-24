"""DBKeyResolver — TTL-cached DB lookup for runtime API key auth.

Plan 8 D8.29 step 3 / Task 22. Sits behind
:class:`gg_relay.api.middleware.api_key_auth.APIKeyAuthMiddleware`
as the production key-resolution path. Replaces the Plan 7
``cfg.api_keys_with_labels`` frozen dict so admin POST / DELETE
on ``/api/v1/admin/keys`` mutate live state without a process
restart.

Performance contract:

  * **Cache** — :class:`cachetools.TTLCache` (default 10s TTL,
    1024 entries). The TTL is deliberately short so a revoke
    propagates to the next request within seconds even without
    an explicit ``invalidate_cache`` hit; the cap caps memory at
    ~tens of KB.
  * **Single-flight** — concurrent misses for the same key_hash
    share one DB lookup via an asyncio Future. This pattern
    avoids a stampede if 50 requests for the same fresh key
    arrive before the first resolver call completes.
  * **Negative caching** — unknown / revoked / expired keys are
    cached as ``None`` so a brute-force probe doesn't translate
    to a DB hit per request. Honours the same TTL as positive
    entries.
  * **Throttled touch_last_used** — at most one ``UPDATE
    api_keys SET last_used_at = now() WHERE key_hash = ?`` per
    key per 60s. Runs as a fire-and-forget background task so the
    middleware never blocks on it.

Role override mode (Plan 8 v2.3 BLOCKER 2):

  * ``"db"`` (default) — ``api_keys.role`` is the source of truth.
    The admin endpoint mutates this column directly.
  * ``"config"`` — ``cfg.role_mapping[label]`` overrides the DB
    column at resolve time. Use ONLY for emergency lockdown where
    an operator wants config-as-code to win over any DB tampering.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from cachetools import TTLCache

from gg_relay.auth.protocol import ResolvedKey
from gg_relay.auth.store import ApiKeyStore, hash_key

logger = logging.getLogger("gg_relay.auth.db_resolver")

# Sentinel returned by ``TTLCache.get`` when the entry truly hasn't
# been populated — distinguishes "no cache entry yet" from "cached
# as ``None`` (negative cache)".
_MISSING = object()


class DBKeyResolver:
    """KeyResolver backed by the ``api_keys`` table with TTL cache.

    Construction args:

      * ``key_store``         — :class:`ApiKeyStore` instance.
      * ``cfg``               — process :class:`Config` (used to
        read ``role_override_mode`` + ``role_mapping`` when the
        operator opts into config-source role override).
      * ``cache_ttl``         — seconds an entry survives in the
        TTL cache. 10s default trades a small amount of stale
        permission for a tight cache miss rate.
      * ``touch_throttle``    — seconds between ``last_used_at``
        updates per key. 60s default keeps the audit grain
        coarse-enough to not bloat the write path.
      * ``role_override_mode`` — ``"db"`` (default) or ``"config"``.
        Passed explicitly so tests can override without mutating
        the global Config.
    """

    def __init__(
        self,
        *,
        key_store: ApiKeyStore,
        cfg: Any = None,
        cache_ttl: float = 10.0,
        touch_throttle: float = 60.0,
        role_override_mode: str = "db",
    ) -> None:
        self._store = key_store
        self._cfg = cfg
        self._cache: TTLCache[str, ResolvedKey | None] = TTLCache(
            maxsize=1024, ttl=cache_ttl,
        )
        self._touch_throttle = touch_throttle
        self._last_touch: dict[str, float] = {}
        self._inflight: dict[str, asyncio.Future[ResolvedKey | None]] = {}
        self._role_override_mode = role_override_mode

    async def resolve(self, raw_key: str) -> ResolvedKey | None:
        """Return :class:`ResolvedKey` if the raw key is valid +
        active + non-expired; otherwise ``None``.

        Hot-path implementation:

          1. Compute the key_hash once.
          2. Probe the TTL cache. ``MISSING`` → fall through;
             cached value (positive or negative) → return.
          3. If another coroutine is already resolving this hash,
             await its Future (single-flight).
          4. Otherwise, lookup → cache → return; clean up the
             in-flight Future on the way out so a thrown exception
             doesn't strand future waiters.
        """
        kh = hash_key(raw_key)
        cached = self._cache.get(kh, _MISSING)
        if cached is not _MISSING:
            if cached is not None:
                self._schedule_touch(kh)
            return cached  # type: ignore[return-value]

        inflight = self._inflight.get(kh)
        if inflight is not None:
            return await inflight

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ResolvedKey | None] = loop.create_future()
        self._inflight[kh] = fut
        try:
            resolved = await self._lookup(kh)
            self._cache[kh] = resolved
            if not fut.done():
                fut.set_result(resolved)
            if resolved is not None:
                self._schedule_touch(kh)
            return resolved
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            self._inflight.pop(kh, None)

    async def _lookup(self, kh: str) -> ResolvedKey | None:
        """Single DB read + revoked/expired filter + role override.

        Lives on the class so test subclasses can patch it (e.g.
        to inject a fake row without round-tripping through the
        store) without monkeypatching the resolver internals.
        """
        row = await self._store.get_by_hash(kh)
        if row is None:
            return None
        if row["revoked_at"] is not None:
            return None
        expires_at = row["expires_at"]
        if expires_at is not None:
            # SQLite stores DateTime(timezone=True) as a naive string
            # and SQLAlchemy hands the value back as a naive datetime.
            # Normalise both sides to UTC so the comparison doesn't
            # raise ``offset-naive vs offset-aware`` TypeError.
            now_utc = datetime.now(UTC)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at < now_utc:
                return None
        role = row["role"]
        if self._role_override_mode == "config" and self._cfg is not None:
            role_map: dict[str, str] = getattr(
                self._cfg, "role_mapping", {}
            ) or {}
            override = role_map.get(row["label"])
            if override:
                role = override
        return ResolvedKey(
            label=row["label"],
            role=role,
            expires_at=row["expires_at"],
            notes=row["notes"],
        )

    def _schedule_touch(self, kh: str) -> None:
        """Fire-and-forget ``last_used_at`` update with 60s throttle.

        We deliberately skip the touch entirely when the
        throttle hasn't elapsed — the column is an observability
        signal, not an audit field, so a small amount of jitter
        is fine.
        """
        now = time.monotonic()
        last = self._last_touch.get(kh, 0.0)
        if now - last < self._touch_throttle:
            return
        self._last_touch[kh] = now
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._safe_touch(kh))

    async def _safe_touch(self, kh: str) -> None:
        try:
            await self._store.touch_last_used(key_hash=kh)
        except Exception:
            logger.debug(
                "touch_last_used failed (kh=%s…)", kh[:8], exc_info=True
            )

    async def invalidate_cache(
        self,
        *,
        key_hash: str | None = None,
        label: str | None = None,
    ) -> None:
        """Drop cache entries for the supplied key.

        ``key_hash`` is the cheapest path (no DB hit). ``label``
        resolves the hash via the store first. Passing neither
        clears everything (used by test teardown / shutdown).
        """
        if key_hash is not None:
            self._cache.pop(key_hash, None)
            self._last_touch.pop(key_hash, None)
            return
        if label is not None:
            row = await self._store.get_by_label(label)
            if row is not None:
                self._cache.pop(row["key_hash"], None)
                self._last_touch.pop(row["key_hash"], None)
            return
        self._cache.clear()
        self._last_touch.clear()
