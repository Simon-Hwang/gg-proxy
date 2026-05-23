"""Unit tests for :class:`TokenBucketRateLimiter` and
:class:`RateLimitMiddleware` (Plan 7 Task 10 / D7.7+D7.8).

The limiter tests drive the algorithm directly so they don't rely on
real wall-clock timing — most of them monkeypatch ``time.monotonic`` so
the suite stays fast and deterministic. The middleware tests build a
minimal Starlette app to assert exempt-path bypass.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gg_relay.api.middleware.rate_limit import (
    RateLimitMiddleware,
    TokenBucketRateLimiter,
)


# ── token bucket algorithm ────────────────────────────────────────────


class TestTokenBucketAlgorithm:
    async def test_bucket_init_full_burst(self) -> None:
        """A fresh bucket starts at ``burst`` tokens."""
        limiter = TokenBucketRateLimiter(rate_per_min=60, burst=5)
        for _ in range(5):
            allowed, retry = await limiter.acquire("k")
            assert allowed is True
            assert retry == 0.0

    async def test_burst_plus_one_returns_429(self) -> None:
        """``burst + 1`` consecutive acquires: last one denied with
        positive retry-after."""
        limiter = TokenBucketRateLimiter(rate_per_min=60, burst=3)
        for _ in range(3):
            ok, _ = await limiter.acquire("k")
            assert ok is True
        ok, retry = await limiter.acquire("k")
        assert ok is False
        assert retry > 0.0

    async def test_refill_after_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Advance monotonic time → tokens refill → next acquire OK."""
        clock = [1000.0]
        monkeypatch.setattr(
            "gg_relay.api.middleware.rate_limit.time.monotonic",
            lambda: clock[0],
        )
        limiter = TokenBucketRateLimiter(rate_per_min=60, burst=1)
        ok, _ = await limiter.acquire("k")
        assert ok is True
        ok, _ = await limiter.acquire("k")
        assert ok is False
        # 1 token / sec refill rate; advance 1.5s
        clock[0] += 1.5
        ok, retry = await limiter.acquire("k")
        assert ok is True
        assert retry == 0.0

    async def test_multi_key_independent(self) -> None:
        """Draining ``k1`` does not affect ``k2``'s bucket."""
        limiter = TokenBucketRateLimiter(rate_per_min=60, burst=2)
        for _ in range(2):
            ok, _ = await limiter.acquire("k1")
            assert ok is True
        denied, _ = await limiter.acquire("k1")
        assert denied is False
        ok, _ = await limiter.acquire("k2")
        assert ok is True

    async def test_100_concurrent_same_key(self) -> None:
        """``asyncio.gather`` of 100 acquires with burst=10 → exactly
        10 succeed. Verifies the per-key lock prevents over-allocation
        from concurrent refill races."""
        limiter = TokenBucketRateLimiter(rate_per_min=1, burst=10)
        results = await asyncio.gather(
            *(limiter.acquire("k") for _ in range(100))
        )
        allowed = sum(1 for ok, _ in results if ok)
        assert allowed == 10

    async def test_per_key_lock_independent(self) -> None:
        """Distinct keys get distinct ``asyncio.Lock`` instances so
        contention on one never blocks another."""
        limiter = TokenBucketRateLimiter(rate_per_min=60, burst=1)
        await limiter.acquire("k1")
        await limiter.acquire("k2")
        assert "k1" in limiter._locks  # noqa: SLF001 — internal API for unit test
        assert "k2" in limiter._locks  # noqa: SLF001
        assert limiter._locks["k1"] is not limiter._locks["k2"]  # noqa: SLF001


# ── eviction ──────────────────────────────────────────────────────────


class TestEviction:
    async def test_lru_cap_evicts_and_syncs_locks(self) -> None:
        """Adding ``lru_cap + 1`` keys never lets either map grow past
        the cap — buckets and locks evict together."""
        cap = 3
        limiter = TokenBucketRateLimiter(
            rate_per_min=60, burst=1, lru_cap=cap
        )
        for i in range(cap + 2):
            await limiter.acquire(f"k{i}")
        # both maps must respect the cap (synchronous _locks cleanup)
        assert len(limiter._buckets) <= cap  # noqa: SLF001
        assert len(limiter._locks) <= cap  # noqa: SLF001
        # the oldest keys (k0, k1) should be the ones evicted
        assert "k0" not in limiter._buckets  # noqa: SLF001
        assert "k0" not in limiter._locks  # noqa: SLF001

    async def test_ttl_sweep_evicts_stale_and_syncs_locks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Driving ``time.monotonic`` past the TTL and calling the
        single-iteration sweep helper drops stale entries from BOTH
        maps; entries within the TTL window survive."""
        clock = [1000.0]
        monkeypatch.setattr(
            "gg_relay.api.middleware.rate_limit.time.monotonic",
            lambda: clock[0],
        )
        limiter = TokenBucketRateLimiter(
            rate_per_min=60, burst=1, ttl_s=10
        )
        await limiter.acquire("stale")  # last_refill=1000
        clock[0] = 1005.0
        await limiter.acquire("fresh")  # last_refill=1005
        clock[0] = 1016.0  # stale=16s (>10), fresh=11s (>10)
        # Touch "fresh" so it stays inside the TTL window.
        limiter._buckets["fresh"].last_refill = clock[0] - 5  # noqa: SLF001
        limiter._sweep_once()  # noqa: SLF001
        assert "stale" not in limiter._buckets  # noqa: SLF001
        assert "stale" not in limiter._locks  # noqa: SLF001
        assert "fresh" in limiter._buckets  # noqa: SLF001
        assert "fresh" in limiter._locks  # noqa: SLF001


# ── middleware: exempt paths ──────────────────────────────────────────


def _ok_handler(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def _build_app(*, limiter: TokenBucketRateLimiter) -> Starlette:
    """Build a minimal ASGI app exposing ``/healthz``, ``/readyz``,
    ``/metrics``, ``/dashboard/foo``, and ``/api/v1/sessions`` so we
    can assert the rate-limit exempt list."""
    routes = [
        Route("/healthz", _ok_handler),
        Route("/readyz", _ok_handler),
        Route("/metrics", _ok_handler),
        Route("/dashboard/foo", _ok_handler),
        Route("/api/v1/sessions", _ok_handler),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(RateLimitMiddleware, limiter=limiter)
    return app


class TestExemptPaths:
    async def test_exempt_paths(self) -> None:
        """``/healthz``, ``/readyz``, ``/metrics`` never touch the
        limiter — even with a fully-drained bucket every request must
        still return 200."""
        limiter = TokenBucketRateLimiter(rate_per_min=1, burst=1)
        # Drain so any non-exempt path would 429.
        await limiter.acquire("k")
        for path in ("/healthz", "/readyz", "/metrics"):
            app = _build_app(limiter=limiter)
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://t"
            ) as ac:
                r = await ac.get(path)
            assert r.status_code == 200, path

    async def test_dashboard_exempt(self) -> None:
        """``/dashboard/...`` is exempt via ``exempt_path_prefixes``."""
        limiter = TokenBucketRateLimiter(rate_per_min=1, burst=0)
        app = _build_app(limiter=limiter)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://t"
        ) as ac:
            r = await ac.get("/dashboard/foo")
        assert r.status_code == 200

    async def test_no_api_key_id_passes_through(self) -> None:
        """Without ``request.state.api_key_id`` the rate-limit
        middleware passes through (preserves auth-layer 401 priority)."""
        limiter = TokenBucketRateLimiter(rate_per_min=1, burst=0)
        app = _build_app(limiter=limiter)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://t"
        ) as ac:
            r = await ac.get("/api/v1/sessions")
        assert r.status_code == 200


# ── lifecycle ─────────────────────────────────────────────────────────


class TestSweepLifecycle:
    async def test_start_sweep_is_idempotent(self) -> None:
        limiter = TokenBucketRateLimiter()
        limiter.start_sweep()
        first = limiter._sweep_task  # noqa: SLF001
        limiter.start_sweep()
        assert limiter._sweep_task is first  # noqa: SLF001
        await limiter.stop()

    async def test_stop_cancels_running_task(self) -> None:
        limiter = TokenBucketRateLimiter()
        limiter.start_sweep()
        task = limiter._sweep_task  # noqa: SLF001
        assert task is not None
        await limiter.stop()
        assert task.cancelled() or task.done()
