"""APIKeyAuthMiddleware via DBKeyResolver path — Plan 8 Task 22 / D8.29.

Validates the new resolver-driven dispatch path on
:class:`APIKeyAuthMiddleware`:

  * Valid key → 200 + request.state populated.
  * Revoked key → 401 with canonical body.
  * Expired key → 401.
  * Missing header → 401.

The middleware now consults ``request.app.state.key_resolver`` first;
these tests inject a minimal mock resolver instead of standing up the
full DB so the middleware contract is verified in isolation.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gg_relay.api.middleware.api_key_auth import APIKeyAuthMiddleware
from gg_relay.auth.protocol import ResolvedKey


class _MockResolver:
    """Plan 7-compatible mock that mirrors ``KeyResolver``.

    Implements the two-method Protocol so the middleware's
    ``app.state.key_resolver`` probe finds a usable object.
    """

    def __init__(self, table: dict[str, ResolvedKey | None]) -> None:
        self._table = table

    async def resolve(self, raw_key: str) -> ResolvedKey | None:
        return self._table.get(raw_key)

    async def invalidate_cache(self, **kw) -> None:  # noqa: D401
        self._table.clear()


def _ok(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "label": getattr(request.state, "api_key_label", None),
            "role": getattr(request.state, "api_key_role", None),
            "hash": getattr(request.state, "api_key_hash", None),
        }
    )


def _build_app(resolver: _MockResolver) -> Starlette:
    app = Starlette(routes=[Route("/api/v1/resource", _ok)])
    app.add_middleware(
        APIKeyAuthMiddleware,
        keys_with_labels={},
        protected_prefix="/api/v1",
    )
    app.state.key_resolver = resolver
    return app


@pytest.mark.asyncio
async def test_valid_key_passes_with_state_populated() -> None:
    resolver = _MockResolver(
        {"rk_alice": ResolvedKey(label="alice", role="admin")}
    )
    app = _build_app(resolver)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/api/v1/resource", headers={"X-API-Key": "rk_alice"}
        )
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == "alice"
    assert body["role"] == "admin"
    assert body["hash"] is not None and len(body["hash"]) == 64


@pytest.mark.asyncio
async def test_revoked_key_returns_401() -> None:
    resolver = _MockResolver({"rk_bye": None})  # negative cache
    app = _build_app(resolver)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/api/v1/resource", headers={"X-API-Key": "rk_bye"}
        )
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid_api_key"}


@pytest.mark.asyncio
async def test_expired_key_returns_401() -> None:
    # Resolver hides expiry — returns None for an expired key just
    # like a revoked one. The middleware contract only needs to see
    # the None → 401 collapse.
    resolver = _MockResolver({"rk_old": None})
    app = _build_app(resolver)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/api/v1/resource", headers={"X-API-Key": "rk_old"}
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_missing_header_returns_401() -> None:
    resolver = _MockResolver(
        {"rk_alice": ResolvedKey(label="alice", role="admin")}
    )
    app = _build_app(resolver)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/api/v1/resource")
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid_api_key"}


# Sanity: even when resolver-aware, the middleware also still serves
# the Plan 7 frozen-dict fallback when no resolver is attached.
@pytest.mark.asyncio
async def test_no_resolver_falls_back_to_keys_with_labels() -> None:
    app = Starlette(routes=[Route("/api/v1/resource", _ok)])
    app.add_middleware(
        APIKeyAuthMiddleware,
        keys_with_labels={"legacy": "legacy-label"},
        protected_prefix="/api/v1",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/api/v1/resource", headers={"X-API-Key": "legacy"}
        )
    assert r.status_code == 200
    assert r.json()["label"] == "legacy-label"
