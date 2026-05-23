"""Plan 8 Task 3 / D8.26 — DashboardCookieMiddleware unit tests.

Exercises the synthetic ``X-API-Key`` injection contract:

1. ``test_cookie_present_injects_x_api_key`` — cookie session
   ``dashboard_user=alice`` + path ``/api/v1/...`` → middleware
   rewrites ``X-API-Key`` to the internal key bound to
   ``dashboard-alice``.
2. ``test_no_cookie_passes_through`` — no cookie → header untouched.
3. ``test_cookie_unknown_user_no_inject`` — cookie carries an unknown
   username → no header rewrite (defends against a stale cookie
   surviving an operator's dashboard_users env change).
4. ``test_dashboard_state_set_for_templates`` — even for non-API
   paths, ``request.state.dashboard_user`` is set so downstream
   handlers / templates can branch.
5. ``test_non_api_path_no_inject`` — cookie valid + path
   ``/dashboard/...`` → ``request.state`` is set but no header
   injection (only ``/api/v1/*`` triggers the rewrite).
6. ``test_existing_x_api_key_overridden`` — cookie identity wins over
   any pre-attached ``X-API-Key`` (single identity contract D8.25).
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gg_relay.api.middleware.dashboard_cookie import (
    SESSION_KEY,
    DashboardCookieMiddleware,
)

_TEST_SESSION_SECRET = "a-test-secret-32-bytes-or-longer-xxxx"


def _build_app(
    *,
    dashboard_internal_keys: dict[str, str] | None = None,
    capture: dict[str, object] | None = None,
    seed_session: dict[str, str] | None = None,
) -> Starlette:
    """Build a tiny Starlette app exercising the middleware in
    isolation. ``capture`` (if provided) receives the resolved
    ``X-API-Key`` header bytes and ``request.state.dashboard_user``
    seen by the route — that's the contract downstream consumers
    (APIKey middleware + templates) rely on.

    ``seed_session`` writes its k/v pairs into ``request.session``
    via a tiny pre-routing middleware so individual tests don't
    have to round-trip through the dashboard login flow to obtain a
    valid cookie. This keeps the unit tests focused on the
    middleware's read-and-inject behaviour rather than on the
    Starlette SessionMiddleware cookie format.
    """
    if capture is None:
        capture = {}

    async def _seed(request: Request, call_next):  # noqa: ANN001
        if seed_session is not None:
            try:
                for k, v in seed_session.items():
                    request.session[k] = v
            except AssertionError:
                pass
        return await call_next(request)

    async def _echo(request: Request) -> JSONResponse:
        capture["x_api_key"] = request.headers.get("X-API-Key")
        capture["dashboard_user"] = getattr(
            request.state, "dashboard_user", None
        )
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/api/v1/sessions", _echo, methods=["GET", "POST"]),
            Route("/dashboard/foo", _echo, methods=["GET"]),
        ]
    )
    # Starlette's ``add_middleware`` wraps in reverse: the LAST call
    # becomes the OUTERMOST layer (dispatched FIRST). We want runtime
    # dispatch order:
    #   SessionMiddleware → _SeedMW → DashboardCookie → route
    # so add in reverse:
    #   1. DashboardCookie (innermost)
    #   2. _SeedMW         (middle — writes test session entries
    #      AFTER SessionMiddleware populated request.session but
    #      BEFORE DashboardCookie reads it)
    #   3. SessionMiddleware (outermost — populates request.session)
    from starlette.middleware.base import BaseHTTPMiddleware

    class _SeedMW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # noqa: ANN001
            return await _seed(request, call_next)

    app.add_middleware(
        DashboardCookieMiddleware,
        dashboard_internal_keys=dashboard_internal_keys or {},
    )
    app.add_middleware(_SeedMW)
    app.add_middleware(
        SessionMiddleware,
        secret_key=_TEST_SESSION_SECRET,
        session_cookie="gg_relay_session",
        same_site="lax",
    )
    return app


@pytest.mark.asyncio
async def test_cookie_present_injects_x_api_key() -> None:
    """Valid cookie + ``/api/v1/*`` → header rewritten to internal key."""
    capture: dict[str, object] = {}
    keys = {"alice": "internal-alice-key-xyz"}
    app = _build_app(
        dashboard_internal_keys=keys,
        capture=capture,
        seed_session={SESSION_KEY: "alice"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/api/v1/sessions")
    assert r.status_code == 200
    assert capture["x_api_key"] == "internal-alice-key-xyz"
    assert capture["dashboard_user"] == "alice"


@pytest.mark.asyncio
async def test_no_cookie_passes_through() -> None:
    """No cookie session → header is exactly whatever the caller sent."""
    capture: dict[str, object] = {}
    keys = {"alice": "internal-alice-key-xyz"}
    app = _build_app(
        dashboard_internal_keys=keys,
        capture=capture,
        seed_session=None,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        # Caller sends their own key — middleware must not touch it.
        r = await ac.get(
            "/api/v1/sessions", headers={"X-API-Key": "operator-key-abc"}
        )
    assert r.status_code == 200
    assert capture["x_api_key"] == "operator-key-abc"
    assert capture["dashboard_user"] is None


@pytest.mark.asyncio
async def test_cookie_unknown_user_no_inject() -> None:
    """Cookie carries an unknown username (e.g. stale session after
    operator removed the user from ``dashboard_users``) → middleware
    does NOT inject and does NOT set ``request.state``."""
    capture: dict[str, object] = {}
    keys = {"alice": "internal-alice-key-xyz"}
    app = _build_app(
        dashboard_internal_keys=keys,
        capture=capture,
        seed_session={SESSION_KEY: "bob"},  # bob is unknown
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(
            "/api/v1/sessions",
            headers={"X-API-Key": "operator-key-abc"},
        )
    assert r.status_code == 200
    assert capture["x_api_key"] == "operator-key-abc"
    assert capture["dashboard_user"] is None


@pytest.mark.asyncio
async def test_dashboard_state_set_for_templates() -> None:
    """Even on a non-API path, valid cookie → state.dashboard_user
    is populated so templates can render "logged in as alice"."""
    capture: dict[str, object] = {}
    keys = {"alice": "internal-alice-key-xyz"}
    app = _build_app(
        dashboard_internal_keys=keys,
        capture=capture,
        seed_session={SESSION_KEY: "alice"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/dashboard/foo")
    assert r.status_code == 200
    assert capture["dashboard_user"] == "alice"


@pytest.mark.asyncio
async def test_non_api_path_no_inject() -> None:
    """Valid cookie + ``/dashboard/*`` → state set, but NO synthetic
    X-API-Key header (only ``/api/v1/*`` triggers injection)."""
    capture: dict[str, object] = {}
    keys = {"alice": "internal-alice-key-xyz"}
    app = _build_app(
        dashboard_internal_keys=keys,
        capture=capture,
        seed_session={SESSION_KEY: "alice"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/dashboard/foo")
    assert r.status_code == 200
    assert capture["dashboard_user"] == "alice"
    # No injection on dashboard paths — header is whatever client sent (nothing).
    assert capture["x_api_key"] is None


@pytest.mark.asyncio
async def test_existing_x_api_key_overridden() -> None:
    """Plan 8 D8.25 — single identity contract: cookie wins. If a
    request carries both a cookie session AND an X-API-Key header,
    the middleware REPLACES the header with the internal key bound
    to the cookie identity so audit / role / owner all agree."""
    capture: dict[str, object] = {}
    keys = {"alice": "internal-alice-key-xyz"}
    app = _build_app(
        dashboard_internal_keys=keys,
        capture=capture,
        seed_session={SESSION_KEY: "alice"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/sessions",
            headers={"X-API-Key": "attacker-supplied-key"},
        )
    assert r.status_code == 200
    # Cookie identity wins; attacker header is dropped.
    assert capture["x_api_key"] == "internal-alice-key-xyz"
    assert capture["x_api_key"] != "attacker-supplied-key"
    assert capture["dashboard_user"] == "alice"
