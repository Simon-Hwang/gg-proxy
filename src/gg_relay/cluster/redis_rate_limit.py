"""Plan 9 D9.2 — RedisRateLimitStore.

Cross-worker token-bucket implementation that satisfies
:class:`gg_relay.core.protocol.RateLimitStoreBackend`. Uses a single
atomic Lua script per ``acquire`` call so the read-refill-decrement
cycle stays consistent across concurrent workers.

Why Lua and not WATCH/MULTI?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

WATCH-based optimistic concurrency loops can starve at high
contention (every worker reads the same key, every CAS retries).
A short Lua script (EVALSHA) executes atomically inside Redis so a
single round-trip = a single bucket update. Trade-off: the script
is somewhat opaque to operators; comments inside the script body
explain each branch.

Why one-hash-per-key vs. one-stream-per-key?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The hash holds ``tokens`` (current bucket level) + ``last_refill``
(unix ms). EXPIRE on the key recycles idle buckets without a
separate sweeper — operator-friendly and avoids the LRU eviction
machinery the in-process variant carries.

Failure semantics
~~~~~~~~~~~~~~~~~

If Redis is unreachable the ``EVAL`` raises and the lifespan's
strict_backend flag decides whether the lifespan dies or falls back
to the in-process limiter. Soft failures (script returns empty
table — should never happen, but defensive) treat the request as
allowed to avoid wedging the API.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import redis.asyncio as aioredis

logger = logging.getLogger("gg_relay.cluster.redis_rate_limit")

# ── Atomic token-bucket Lua script ──────────────────────────────────
# KEYS[1] — hash key (e.g. "gg-relay:rl:<api_key_id>")
# ARGV[1] — current unix-ms timestamp
# ARGV[2] — burst capacity (max tokens)
# ARGV[3] — refill rate (tokens per second; float)
# ARGV[4] — TTL seconds for the key (resets idle buckets)
# Returns: { allowed: 0|1, retry_after_ms: int }
_LUA_ACQUIRE = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local refill_rate_per_s = tonumber(ARGV[3])
local ttl_s = tonumber(ARGV[4])

-- Read existing state (or initialise to full bucket).
local state = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(state[1])
local last_refill = tonumber(state[2])
if tokens == nil then
  tokens = burst
  last_refill = now
end

-- Refill: tokens accrue linearly between calls, capped at burst.
local elapsed_s = (now - last_refill) / 1000.0
tokens = math.min(burst, tokens + elapsed_s * refill_rate_per_s)
last_refill = now

local allowed = 0
local retry_after_ms = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  -- Compute how many ms until 1 full token is available.
  local deficit = 1 - tokens
  retry_after_ms = math.ceil((deficit / refill_rate_per_s) * 1000)
end

redis.call('HMSET', key, 'tokens', tokens, 'last_refill', last_refill)
redis.call('EXPIRE', key, ttl_s)

return { allowed, retry_after_ms }
"""


class RedisRateLimitStore:
    """Multi-worker token-bucket store backed by atomic Lua scripts.

    Constructor signature mirrors
    :class:`gg_relay.api.middleware.rate_limit.TokenBucketRateLimiter`
    so the lifespan can swap implementations without code changes at
    the middleware layer. The ``key_prefix`` defends against namespace
    collision when multiple gg-relay clusters share one Redis (e.g.
    staging + prod against the same ElastiCache).
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        rate_per_min: int = 60,
        burst: int = 60,
        ttl_s: int = 3600,
        key_prefix: str = "gg-relay:rl",
    ) -> None:
        self._redis = redis
        self._refill_rate = rate_per_min / 60.0
        self._burst = burst
        self._ttl = ttl_s
        self._key_prefix = key_prefix
        self._script_sha: str | None = None
        # Snapshot the script for register_script so the Redis client
        # caches the SHA on first call (subsequent calls use EVALSHA
        # automatically and re-register on NOSCRIPT).
        self._script = self._redis.register_script(_LUA_ACQUIRE)

    async def acquire(self, key: str) -> tuple[bool, float]:
        """Try to spend a token for ``key`` via atomic EVALSHA.

        Returns ``(allowed, retry_after_seconds)`` to match the
        Protocol; converts the script's ms retry-after to seconds.
        """
        full_key = f"{self._key_prefix}:{key}"
        now_ms = int(time.time() * 1000)
        try:
            result = await self._script(
                keys=[full_key],
                args=[now_ms, self._burst, self._refill_rate, self._ttl],
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.exception("redis_rate_limit.acquire_failed key=%s", key)
            _bump("REDIS_RATE_LIMIT_EVAL_ERRORS_TOTAL")
            # Fail-open so a transient Redis blip doesn't 429 every
            # request. The lifespan's strict_backend health check
            # already aborts on persistent failures.
            return True, 0.0
        if not result or len(result) < 2:
            return True, 0.0
        allowed = bool(int(result[0]))
        retry_after_ms = int(result[1])
        _bump(
            "REDIS_RATE_LIMIT_ALLOWED_TOTAL"
            if allowed
            else "REDIS_RATE_LIMIT_DENIED_TOTAL"
        )
        return allowed, retry_after_ms / 1000.0


def _bump(metric_name: str) -> None:
    """Best-effort Prometheus increment; never raises into the caller."""
    try:
        from gg_relay.tracing import metrics

        metric = getattr(metrics, metric_name, None)
        if metric is not None:
            metric.inc()
    except Exception:  # noqa: BLE001 — defensive
        pass


__all__ = ["RedisRateLimitStore"]
