"""Verify APIKey → RateLimit dispatch order (Plan 7 Task 10).

Two scenarios matter:

1. No ``X-API-Key`` header → ``APIKeyAuthMiddleware`` 401s before the
   rate-limit middleware ever sees the request, so even a fully-drained
   bucket cannot accidentally answer 429.
2. Valid header + bucket exhausted → 429 with ``Retry-After`` header.
"""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gg_relay.api.middleware.api_key_auth import APIKeyAuthMiddleware
from gg_relay.api.middleware.rate_limit import (
    RateLimitMiddleware,
    TokenBucketRateLimiter,
)


async def _ok(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def _build_app(
    *,
    limiter: TokenBucketRateLimiter,
    keys: tuple[str, ...] = ("k1",),
) -> Starlette:
    """Wire APIKey + RateLimit in the same order as
    :func:`gg_relay.api.main.create_app` so the test app exercises the
    real dispatch flow:

    * ``add_middleware`` 1st (innermost): RateLimit
    * ``add_middleware`` 2nd (outermost): APIKey

    Dispatch order at runtime: APIKey → RateLimit → handler.
    """
    routes = [Route("/api/v1/sessions", _ok)]
    app = Starlette(routes=routes)
    app.add_middleware(RateLimitMiddleware, limiter=limiter)
    app.add_middleware(
        APIKeyAuthMiddleware,
        expected_keys=keys,
        protected_prefix="/api/v1",
    )
    return app


async def test_no_api_key_returns_401_not_429() -> None:
    """Even when the limiter would have 429'd everyone, a missing
    header still produces 401 — APIKey runs before RateLimit."""
    limiter = TokenBucketRateLimiter(rate_per_min=1, burst=0)
    app = _build_app(limiter=limiter)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/api/v1/sessions")
    assert r.status_code == 401


async def test_authed_excess_returns_429() -> None:
    """Valid key + bucket exhausted → 429 with positive Retry-After."""
    limiter = TokenBucketRateLimiter(rate_per_min=60, burst=2)
    app = _build_app(limiter=limiter)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        for _ in range(2):
            r = await ac.get(
                "/api/v1/sessions", headers={"X-API-Key": "k1"}
            )
            assert r.status_code == 200
        r = await ac.get(
            "/api/v1/sessions", headers={"X-API-Key": "k1"}
        )
    assert r.status_code == 429
    assert r.json()["detail"] == "rate_limit_exceeded"
    retry_after = r.headers.get("Retry-After")
    assert retry_after is not None
    assert int(retry_after) >= 1
