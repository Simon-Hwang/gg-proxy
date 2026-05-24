"""Plan 9 v0.9.0-rc D9.0a — DashboardCookieMiddleware app.state path.

The Plan 9.1 D9.10 DB-backed dashboard key swap requires the lifespan
to be able to *replace* the ``{username: raw_key}`` mapping after
``create_app()`` has wired the middleware chain. FastAPI forbids
``add_middleware`` after lifespan start, so the middleware must read
the mapping at *request* time from ``app.state.dashboard_internal_keys``
rather than from a constructor-frozen dict.

These tests cover:

1. ``app.state.dashboard_internal_keys`` populated → middleware reads
   from it (no ctor kwarg required).
2. Lifespan-time reassignment of ``app.state.dashboard_internal_keys``
   takes effect on subsequent requests (the Plan 9.1 D9.10 rotation
   use case).
3. ``app.state`` empty AND no legacy ctor kwarg → middleware passes
   through (no header injection, no exception).
4. ctor kwarg + ``app.state`` both present → ``app.state`` wins.
5. Legacy ctor kwarg path still works when ``app.state`` is absent
   (back-compat with v0.8.x unit tests).
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
    legacy_ctor_keys: dict[str, str] | None = None,
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

    # innermost → outermost (Starlette reverses add order)
    if legacy_ctor_keys is not None:
        app.add_middleware(
            DashboardCookieMiddleware,
            dashboard_internal_keys=legacy_ctor_keys,
        )
    else:
        app.add_middleware(DashboardCookieMiddleware)
    app.add_middleware(_SeedMW)
    app.add_middleware(
        SessionMiddleware,
        secret_key=_SECRET,
        session_cookie="gg_relay_session",
        same_site="lax",
    )
    # Mirror the create_app contract: pre-populate app.state
    # with the dashboard_internal_keys mapping BEFORE the test
    # request reaches the middleware.
    if state_keys is not None:
        app.state.dashboard_internal_keys = state_keys
    return app


@pytest.mark.asyncio
async def test_app_state_mapping_used_when_present() -> None:
    """Lifespan-populated app.state mapping drives injection."""
    capture: dict[str, object] = {}
    app = _build_app(
        state_keys={"alice": "state-key-1"},
        legacy_ctor_keys=None,
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
    """Rotating the mapping at lifespan time (Plan 9.1 D9.10) is
    reflected by subsequent requests — proves the middleware reads
    runtime, not ctor-frozen."""
    capture: dict[str, object] = {}
    app = _build_app(
        state_keys={"alice": "key-before-rotation"},
        legacy_ctor_keys=None,
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
async def test_no_state_no_ctor_no_injection() -> None:
    """Missing app.state AND no ctor kwarg → middleware passes through.

    Defensive: regression-guards against AttributeError or KeyError
    when an operator forgets to populate app.state.
    """
    capture: dict[str, object] = {}
    app = _build_app(
        state_keys=None,
        legacy_ctor_keys=None,
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
    # dashboard_user is also not set because the username isn't in the
    # (empty) mapping.
    assert capture["dashboard_user"] is None


@pytest.mark.asyncio
async def test_app_state_wins_over_legacy_ctor() -> None:
    """When both sources are present, app.state takes precedence —
    the lifespan is the source of truth."""
    capture: dict[str, object] = {}
    app = _build_app(
        state_keys={"alice": "from-state"},
        legacy_ctor_keys={"alice": "from-ctor"},
        capture=capture,
        seed_session={SESSION_KEY: "alice"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/api/v1/sessions")
    assert r.status_code == 200
    assert capture["x_api_key"] == "from-state"


@pytest.mark.asyncio
async def test_legacy_ctor_still_works_without_app_state() -> None:
    """Back-compat: v0.8.x test fixtures that build the middleware
    standalone (no app.state) must keep working unchanged."""
    capture: dict[str, object] = {}
    app = _build_app(
        state_keys=None,
        legacy_ctor_keys={"alice": "ctor-fallback-key"},
        capture=capture,
        seed_session={SESSION_KEY: "alice"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/api/v1/sessions")
    assert r.status_code == 200
    assert capture["x_api_key"] == "ctor-fallback-key"
