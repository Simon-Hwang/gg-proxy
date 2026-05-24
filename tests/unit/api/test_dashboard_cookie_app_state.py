"""Plan 9 D9.0a — DashboardCookieMiddleware app.state path.

The Plan 9 D9.10 DB-backed dashboard key swap requires the lifespan
to be able to *replace* the ``{username: raw_key}`` mapping after
``create_app()`` has wired the middleware chain. FastAPI forbids
``add_middleware`` after lifespan start, so the middleware reads
the mapping at *request* time from
``app.state.dashboard_internal_keys`` rather than from a
constructor-frozen dict.

Pre-production simplification (v0.9.0): the legacy ctor kwarg path
was removed entirely. The middleware now has a single source of
truth — ``app.state``.

Tests cover:

1. ``app.state.dashboard_internal_keys`` populated → middleware
   reads from it.
2. Lifespan-time reassignment takes effect on subsequent requests
   (the D9.10 rotation use case).
3. ``app.state`` empty → middleware passes through (no injection,
   no exception).
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gg_relay.api.middleware.dashboard_cookie import (
    SESSION_KEY,
    DashboardCookieMiddleware,
)

_SECRET = "a-test-secret-32-bytes-or-longer-xxxx"


def _build_app(
    *,
    state_keys: dict[str, str] | None,
    capture: dict[str, object] | None = None,
    seed_session: dict[str, str] | None = None,
) -> Starlette:
    if capture is None:
        capture = {}

    async def _echo(request: Request) -> JSONResponse:
        capture["x_api_key"] = request.headers.get("X-API-Key")
        capture["dashboard_user"] = getattr(
            request.state, "dashboard_user", None
        )
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/api/v1/sessions", _echo, methods=["GET", "POST"]),
        ]
    )

    class _SeedMW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # noqa: ANN001
            if seed_session is not None:
                try:
                    for k, v in seed_session.items():
                        request.session[k] = v
                except AssertionError:
                    pass
            return await call_next(request)

    app.add_middleware(DashboardCookieMiddleware)
    app.add_middleware(_SeedMW)
    app.add_middleware(
        SessionMiddleware,
        secret_key=_SECRET,
        session_cookie="gg_relay_session",
        same_site="lax",
    )
    if state_keys is not None:
        app.state.dashboard_internal_keys = state_keys
    return app


@pytest.mark.asyncio
async def test_app_state_mapping_used_when_present() -> None:
    """Lifespan-populated app.state mapping drives injection."""
    capture: dict[str, object] = {}
    app = _build_app(
        state_keys={"alice": "state-key-1"},
        capture=capture,
        seed_session={SESSION_KEY: "alice"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/api/v1/sessions")
    assert r.status_code == 200
    assert capture["x_api_key"] == "state-key-1"
    assert capture["dashboard_user"] == "alice"


@pytest.mark.asyncio
async def test_app_state_reassignment_takes_effect() -> None:
    """Rotating the mapping at lifespan time (D9.10) is reflected by
    subsequent requests — proves the middleware reads runtime, not
    ctor-frozen."""
    capture: dict[str, object] = {}
    app = _build_app(
        state_keys={"alice": "key-before-rotation"},
        capture=capture,
        seed_session={SESSION_KEY: "alice"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r1 = await ac.get("/api/v1/sessions")
        assert capture["x_api_key"] == "key-before-rotation"
        # Simulate D9.10 admin rotation: lifespan swaps the mapping.
        app.state.dashboard_internal_keys = {"alice": "key-after-rotation"}
        r2 = await ac.get("/api/v1/sessions")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert capture["x_api_key"] == "key-after-rotation"


@pytest.mark.asyncio
async def test_no_state_no_injection() -> None:
    """Missing app.state → middleware passes through.

    Defensive: regression-guards against AttributeError when an
    operator forgets to populate app.state.
    """
    capture: dict[str, object] = {}
    app = _build_app(
        state_keys=None,
        capture=capture,
        seed_session={SESSION_KEY: "alice"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/api/v1/sessions", headers={"X-API-Key": "operator-key"}
        )
    assert r.status_code == 200
    # Header is whatever the caller sent (no injection happened).
    assert capture["x_api_key"] == "operator-key"
    # dashboard_user is also not set because the mapping is empty.
    assert capture["dashboard_user"] is None
