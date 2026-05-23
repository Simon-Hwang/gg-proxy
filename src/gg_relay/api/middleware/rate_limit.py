"""Token-bucket rate limit middleware (Plan 7 Task 10 / D7.7+D7.8).

Provides an in-process per-API-key token-bucket limiter and a Starlette
middleware that 429s requests once a key exhausts its bucket.

Design notes:

* Each ``api_key_id`` gets its own :class:`asyncio.Lock` so concurrent
  requests for the same key serialise on the bucket update without
  blocking other keys.
* ``_buckets`` and ``_locks`` are LRU-ordered (``OrderedDict``) and stay
  in sync — both eviction paths (LRU cap when adding a new key, and the
  TTL sweep) clean both maps so locks never leak.
* The middleware honours an explicit exempt set (``/healthz``,
  ``/readyz``, ``/metrics``) and configurable path prefixes (the
  dashboard by default). When ``request.state.api_key_id`` is unset
  the middleware passes through — APIKey auth has either already
  short-circuited with a 401 or the route is intentionally public, so
  we never want to overwrite that with a 429.
* Plan 8 D8.2 will swap this for a Redis-backed implementation; the
  middleware keeps the same surface so the swap is local.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

logger = logging.getLogger("gg_relay.api.rate_limit")

_CallNext = Callable[[Request], Awaitable[Response]]


@dataclass
class _Bucket:
    """Token bucket state for a single API key."""

    tokens: float
    last_refill: float


class TokenBucketRateLimiter:
    """Per-key token-bucket limiter with LRU + TTL eviction.

    ``rate_per_min`` controls the refill rate; ``burst`` the bucket
    size. ``lru_cap`` bounds in-memory state by evicting the
    least-recently-used bucket when a new key would overflow it.
    ``ttl_s`` is the idle window the periodic sweeper uses to drop
    buckets that have not been refilled.
    """

    def __init__(
        self,
        *,
        rate_per_min: int = 60,
        burst: int = 60,
        lru_cap: int = 10_000,
        ttl_s: int = 3600,
    ) -> None:
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._refill_rate = rate_per_min / 60.0
        self._burst = float(burst)
        self._lru_cap = lru_cap
        self._ttl = ttl_s
        self._sweep_task: asyncio.Task[None] | None = None

    def start_sweep(self) -> None:
        """Schedule the periodic sweeper. Idempotent — safe to call
        multiple times; only one task runs at a time."""
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.create_task(
                self._sweep(), name="rate-limit-sweep"
            )

    async def stop(self) -> None:
        """Cancel the sweeper task and wait for it to exit."""
        task = self._sweep_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - defensive
                logger.exception("rate-limit sweep task raised on shutdown")
        self._sweep_task = None

    async def _sweep(self) -> None:
        """Background loop: every minute, drop buckets idle past TTL."""
        while True:
            await asyncio.sleep(60)
            try:
                self._sweep_once()
            except Exception:  # pragma: no cover - defensive
                logger.exception("rate-limit sweep iteration failed")

    def _sweep_once(self) -> None:
        """Drop any bucket whose ``last_refill`` is older than ``ttl_s``.

        Extracted so tests can drive a single iteration deterministically
        instead of waiting on the 60-second loop.
        """
        now = time.monotonic()
        stale = [
            k
            for k, b in list(self._buckets.items())
            if now - b.last_refill > self._ttl
        ]
        for k in stale:
            self._buckets.pop(k, None)
            self._locks.pop(k, None)

    def _evict_lru(self) -> None:
        """Evict the LRU bucket if at capacity — synchronously cleans
        the matching lock so locks never leak past their bucket."""
        if len(self._buckets) >= self._lru_cap:
            evicted_key, _ = self._buckets.popitem(last=False)
            self._locks.pop(evicted_key, None)

    async def acquire(self, key: str) -> tuple[bool, float]:
        """Try to spend a token for ``key``.

        Returns ``(allowed, retry_after_seconds)``. ``retry_after_seconds``
        is ``0`` when allowed; otherwise the time until at least one
        token is available again.
        """
        if key not in self._locks:
            self._evict_lru()
            self._locks[key] = asyncio.Lock()
        self._locks.move_to_end(key)
        lock = self._locks[key]
        async with lock:
            now = time.monotonic()
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(self._burst, now)
                self._buckets[key] = b
            self._buckets.move_to_end(key)
            elapsed = now - b.last_refill
            b.tokens = min(self._burst, b.tokens + elapsed * self._refill_rate)
            b.last_refill = now
            if b.tokens >= 1:
                b.tokens -= 1
                return True, 0.0
            return False, (1 - b.tokens) / self._refill_rate


class RateLimitMiddleware(BaseHTTPMiddleware):
    """429 requests once their API key bucket is exhausted.

    Path-level exemptions:

    * Hard-coded ``EXEMPT`` set covers liveness/readiness/metrics so
      probes never count against quota.
    * ``exempt_path_prefixes`` adds the dashboard (and any operator-
      configured prefix) to the bypass list.

    Auth ordering:

    The middleware MUST be wrapped by :class:`APIKeyAuthMiddleware` so
    ``request.state.api_key_id`` is populated before dispatch reaches
    here. When the field is missing we pass through — the auth layer
    has either short-circuited with a 401 (preserve that priority) or
    the route is intentionally unauthenticated (e.g. dashboard, health).
    """

    EXEMPT: frozenset[str] = frozenset({"/healthz", "/readyz", "/metrics"})

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: TokenBucketRateLimiter,
        exempt_path_prefixes: Iterable[str] = ("/dashboard/",),
    ) -> None:
        super().__init__(app)
        self._limiter = limiter
        self._exempt_prefixes = tuple(exempt_path_prefixes)

    async def dispatch(
        self,
        request: Request,
        call_next: _CallNext,
    ) -> Response:
        path = request.url.path
        if path in self.EXEMPT or any(
            path.startswith(pre) for pre in self._exempt_prefixes
        ):
            return await call_next(request)
        key_id = getattr(request.state, "api_key_id", None)
        if not key_id:
            return await call_next(request)
        allowed, retry_after = await self._limiter.acquire(key_id)
        if not allowed:
            retry_s = int(retry_after) + 1
            return JSONResponse(
                {
                    "detail": "rate_limit_exceeded",
                    "retry_after_seconds": retry_s,
                },
                status_code=429,
                headers={"Retry-After": str(retry_s)},
            )
        return await call_next(request)
