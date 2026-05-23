"""Plan 7 Task 11 (D7.15) — ``request.state.api_key_id`` is a hash.

The plaintext API key MUST NOT leak into ``request.state`` or into
downstream code; the middleware stores only ``sha256(key)[:16]`` and
:func:`api.deps.get_api_key_id` consumes that hash. Two checks here:

  1. Behavioural: a real request through the middleware exposes the
     correct sha256 prefix on ``request.state.api_key_id`` and the
     plaintext key never appears on that attribute.
  2. Source-level: ``api/deps.py`` never reads ``X-API-Key`` from the
     request headers (we use the state-attribute hash instead).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gg_relay.api.deps import get_api_key_id
from gg_relay.api.middleware.api_key_auth import APIKeyAuthMiddleware


@pytest.mark.asyncio
async def test_api_key_id_is_sha256_hash_not_plaintext() -> None:
    """Submit a request with a known API key; assert ``api_key_id`` is
    ``sha256(key).hexdigest()[:16]`` AND not equal to the plaintext."""
    key = "supersecret-12345"
    captured: dict[str, object] = {}

    async def _route(request: Request) -> JSONResponse:
        captured["api_key_id"] = getattr(request.state, "api_key_id", None)
        captured["api_key_label"] = getattr(
            request.state, "api_key_label", None
        )
        # Also exercise the dependency the routers see.
        captured["from_deps"] = get_api_key_id(request)
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/api/v1/resource", _route)])
    app.add_middleware(
        APIKeyAuthMiddleware,
        keys_with_labels={key: "alice"},
        protected_prefix="/api/v1",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/api/v1/resource", headers={"X-API-Key": key}
        )
    assert r.status_code == 200
    expected_hash = hashlib.sha256(key.encode()).hexdigest()[:16]
    assert captured["api_key_id"] == expected_hash
    assert captured["api_key_id"] != key
    # The dependency must surface the same hash (not the plaintext).
    assert captured["from_deps"] == expected_hash
    assert captured["from_deps"] != key
    # Label still flows through for owner attribution (Task 6b).
    assert captured["api_key_label"] == "alice"


def test_deps_module_does_not_read_plaintext_header() -> None:
    """Static check: ``api/deps.py`` MUST NOT call
    ``request.headers["X-API-Key"]`` or ``request.headers.get("X-API-Key")``.
    The dependency reads the sha256 hash from ``request.state``
    instead — see :func:`gg_relay.api.deps.get_api_key_id`.
    """
    src = (
        Path(__file__).resolve().parents[3]
        / "src/gg_relay/api/deps.py"
    ).read_text(encoding="utf-8")
    # ``X-API-Key`` only appears in module-level docstrings explaining
    # the historical behaviour (if at all). Any header-read access
    # patterns are banned.
    banned = (
        'headers["X-API-Key"]',
        "headers['X-API-Key']",
        'headers.get("X-API-Key")',
        "headers.get('X-API-Key')",
        'headers["x-api-key"]',
        "headers['x-api-key']",
        'headers.get("x-api-key")',
        "headers.get('x-api-key')",
    )
    for pattern in banned:
        assert pattern not in src, (
            f"api/deps.py contains banned plaintext header access "
            f"pattern: {pattern!r}"
        )


@pytest.mark.asyncio
async def test_webhook_prefix_exempt_from_api_key_auth() -> None:
    """Plan 7 Task 11 (D7.15) — webhook routes under
    ``/api/v1/webhooks/`` and ``/im/`` bypass the API-key check so
    Feishu (which can't send ``X-API-Key`` on callbacks) reaches the
    canonical webhook router. Without this, Task 12's canonical path
    would 401 every production Feishu callback.
    """
    from gg_relay.api.middleware.api_key_auth import WEBHOOK_EXEMPT_PREFIXES

    # The constant itself is the contract Task 12 depends on.
    assert "/api/v1/webhooks/" in WEBHOOK_EXEMPT_PREFIXES
    assert "/im/" in WEBHOOK_EXEMPT_PREFIXES

    async def _route(request: Request) -> JSONResponse:
        del request
        return JSONResponse({"reached": True})

    app = Starlette(
        routes=[
            Route("/api/v1/webhooks/feishu", _route, methods=["POST"]),
            Route("/im/feishu/callback", _route, methods=["POST"]),
            Route("/api/v1/sessions", _route, methods=["GET"]),
        ]
    )
    app.add_middleware(
        APIKeyAuthMiddleware,
        keys_with_labels={"k1": "alice"},
        protected_prefix="/api/v1",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        # Webhook routes pass with NO X-API-Key.
        r_canonical = await ac.post("/api/v1/webhooks/feishu")
        r_alias = await ac.post("/im/feishu/callback")
        # Sibling /api/v1 route still requires it.
        r_protected = await ac.get("/api/v1/sessions")
    assert r_canonical.status_code == 200
    assert r_canonical.json() == {"reached": True}
    assert r_alias.status_code == 200
    assert r_alias.json() == {"reached": True}
    assert r_protected.status_code == 401


@pytest.mark.asyncio
async def test_get_api_key_id_returns_none_when_unauthed() -> None:
    """When the middleware is bypassed (e.g. ``allow_no_keys=True``
    test paths) ``request.state.api_key_id`` is unset and the
    dependency returns ``None`` — NOT the plaintext header.
    """
    captured: dict[str, object] = {}

    async def _route(request: Request) -> JSONResponse:
        captured["from_deps"] = get_api_key_id(request)
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/api/v1/resource", _route)])
    app.add_middleware(
        APIKeyAuthMiddleware,
        keys_with_labels={},
        protected_prefix="/api/v1",
        allow_no_keys=True,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/api/v1/resource", headers={"X-API-Key": "plaintext-key"}
        )
    assert r.status_code == 200
    # The dependency MUST NOT echo the plaintext header back.
    assert captured["from_deps"] is None
