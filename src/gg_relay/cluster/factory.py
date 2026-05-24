"""Plan 9 D9.3 — backend factories invoked by the lifespan.

Centralises the "in-memory vs Redis" decision so the lifespan
(``api/main.py``) stays a single-line call site per backend:

    bus, redis_client = await build_event_bus(cfg)
    limiter = await build_rate_limit_store(cfg, redis_client)

The factory owns:

* Redis client construction (one shared client per backend tier so
  the bus + rate-limit store don't open two connections).
* TLS / Sentinel detection from the ``redis_url`` scheme.
* Strict-mode failure policy — when ``cfg.strict_backend=True`` a
  Redis connection error aborts the lifespan; when False it logs
  and falls back to the in-process implementation.
* Health probe ping at construction time so a misconfigured URL
  surfaces immediately rather than on the first publish/acquire.

Why a separate factory module (not the lifespan):

The lifespan is already 300+ lines of one-shot wiring. A factory
makes the decision logic testable in isolation (no app/server
fixture) and lets the K8sJobExecutor (D9.8) reuse the same Redis
client when it ships.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gg_relay.api.middleware.rate_limit import TokenBucketRateLimiter
from gg_relay.cluster.redis_bus import RedisStreamEventBus
from gg_relay.cluster.redis_rate_limit import RedisRateLimitStore
from gg_relay.cluster.wire import STREAM_KEY
from gg_relay.core.event_bus import EventBus

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from gg_relay.config import Config
    from gg_relay.core.protocol import EventBusBackend, RateLimitStoreBackend

logger = logging.getLogger("gg_relay.cluster.factory")


class RedisUnavailableError(RuntimeError):
    """Raised in strict mode when the configured Redis is unreachable."""


async def _build_redis_client(cfg: Config) -> aioredis.Redis | None:
    """Open and ping a Redis connection; return None on failure when
    not in strict mode.

    Uses ``decode_responses=True`` so the bus + rate-limit store
    receive ``dict[str, str]`` from XREAD / HMGET. The ping is a
    fail-fast guard so a misconfigured URL surfaces here (lifespan
    abort) rather than 1000 requests later when the first user hits
    the API.
    """
    if not cfg.redis_url:
        return None
    try:
        import redis.asyncio as aioredis
    except ImportError as exc:
        if cfg.strict_backend:
            raise RedisUnavailableError(
                "redis-py is not installed but cfg.redis_url is set "
                "and strict_backend=True. Install the 'redis' extra: "
                "pip install 'gg-relay[redis]'"
            ) from exc
        logger.warning(
            "redis-py is not installed; falling back to in-process "
            "backends. Set strict_backend=true to fail-fast instead."
        )
        return None
    # mypy: redis-py's from_url is untyped, so we cast the result.
    client = aioredis.from_url(  # type: ignore[no-untyped-call]
        cfg.redis_url,
        decode_responses=True,
        socket_timeout=getattr(cfg, "redis_socket_timeout", 5.0),
    )
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001 — defensive
        await client.aclose()
        if cfg.strict_backend:
            raise RedisUnavailableError(
                f"Redis ping failed: {exc!r}; aborting lifespan because "
                "strict_backend=True"
            ) from exc
        logger.warning(
            "redis.ping_failed err=%r; falling back to in-process "
            "backends. Set strict_backend=true to fail-fast.",
            exc,
        )
        return None
    return client  # type: ignore[no-any-return]


async def build_event_bus(
    cfg: Config,
    *,
    redis_client: aioredis.Redis | None = None,
    durable_store: Any = None,
    on_drop: Any = None,
    on_durable_drop: Any = None,
) -> tuple[EventBusBackend, aioredis.Redis | None]:
    """Build the event bus + return the underlying Redis client.

    Returns ``(bus, client)`` so the caller can share the client
    with :func:`build_rate_limit_store` and close it once at
    lifespan shutdown. ``client`` is ``None`` for the in-process
    bus.
    """
    if cfg.event_bus_backend != "redis":
        bus: EventBusBackend = EventBus(
            on_drop=on_drop,
            on_durable_drop=on_durable_drop,
            durable_store=durable_store,
        )
        return bus, None

    if redis_client is None:
        redis_client = await _build_redis_client(cfg)
    if redis_client is None:
        # Fall back to in-process (strict_backend=False path)
        bus = EventBus(
            on_drop=on_drop,
            on_durable_drop=on_durable_drop,
            durable_store=durable_store,
        )
        return bus, None

    stream_key = getattr(cfg, "redis_stream_key", STREAM_KEY)
    redis_bus = RedisStreamEventBus(redis_client, stream_key=stream_key)
    logger.info(
        "event_bus.backend=redis stream_key=%s url=%s",
        stream_key,
        cfg.redis_url,
    )
    return redis_bus, redis_client


async def build_rate_limit_store(
    cfg: Config,
    redis_client: aioredis.Redis | None = None,
) -> tuple[RateLimitStoreBackend, aioredis.Redis | None]:
    """Build the rate-limit store. Reuses ``redis_client`` if given.

    Constructor parameters (rate_per_min, burst, ttl_s) match the
    Plan 7 ``TokenBucketRateLimiter`` so the swap is transparent
    to the middleware layer (``RateLimitMiddleware`` just forwards
    to ``acquire``).
    """
    rate_per_min = getattr(cfg, "rate_limit_per_min", 60)
    burst = getattr(cfg, "rate_limit_burst", 60)

    if cfg.rate_limit_backend != "redis":
        limiter: RateLimitStoreBackend = TokenBucketRateLimiter(
            rate_per_min=rate_per_min, burst=burst
        )
        return limiter, None

    if redis_client is None:
        redis_client = await _build_redis_client(cfg)
    if redis_client is None:
        limiter = TokenBucketRateLimiter(
            rate_per_min=rate_per_min, burst=burst
        )
        return limiter, None

    redis_limiter = RedisRateLimitStore(
        redis_client,
        rate_per_min=rate_per_min,
        burst=burst,
    )
    logger.info(
        "rate_limit.backend=redis rate_per_min=%d burst=%d",
        rate_per_min,
        burst,
    )
    return redis_limiter, redis_client


__all__ = [
    "RedisUnavailableError",
    "build_event_bus",
    "build_rate_limit_store",
]
