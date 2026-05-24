"""Plan 8 Task 3 / D8.25 — single identity contract assertions.

The Plan 8 invariant is:

    ``request.state.api_key_label`` is the ONE actor identity that
    every downstream consumer (owner attribution, role lookup, audit
    log, alert mention) reads. The two known paths that populate this
    field are:

    * API client with ``X-API-Key`` header → label is the operator
      tag from ``cfg.api_keys_with_labels`` (parsed via D7.26
      ``label=key`` / ``key:label`` shapes), e.g. ``"alice"``.
    * Dashboard browser session cookie →
      :class:`DashboardCookieMiddleware` rewrites the synthetic
      ``X-API-Key`` to the internal key whose label is
      ``"dashboard-<username>"``, so the downstream
      :class:`APIKeyAuthMiddleware` resolves the same field name
      with that namespaced label.

Both paths feed the same ``request.state.api_key_label`` attribute,
so any downstream consumer (audit row writer, role gate, owner
attribution) only needs to read that one field. This file pins
that invariant with two end-to-end middleware-chain assertions.
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

from gg_relay.api.middleware.api_key_auth import APIKeyAuthMiddleware
from gg_relay.api.middleware.dashboard_cookie import (
    SESSION_KEY,
    DashboardCookieMiddleware,
)

_TEST_SESSION_SECRET = "a-test-secret-32-bytes-or-longer-xxxx"


def _build_full_chain_app(
    *,
    keys_with_labels: dict[str, str],
    dashboard_internal_keys: dict[str, str] | None = None,
    capture: dict[str, object] | None = None,
    seed_session: dict[str, str] | None = None,
) -> Starlette:
    """Stand up an app with the real middleware chain plus a seed
    layer so individual tests can drop a value into ``request.session``
    without a full dashboard-login round-trip.

    Dispatch order at runtime (matches ``create_app`` modulo the
    Logging/RateLimit layers, which don't affect identity):

        SessionMiddleware → _SeedMW → DashboardCookie → APIKey → route

    Add order is therefore the reverse:
      1. APIKey            (innermost, runs after DashboardCookie)
      2. DashboardCookie   (rewrites X-API-Key before APIKey reads it)
      3. _SeedMW           (writes test seed entries into the live
                            request.session before DashboardCookie)
      4. SessionMiddleware (outermost, populates request.session)
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
        capture["api_key_label"] = getattr(
            request.state, "api_key_label", None
        )
        capture["api_key_id"] = getattr(
            request.state, "api_key_id", None
        )
        capture["dashboard_user"] = getattr(
            request.state, "dashboard_user", None
        )
        return JSONResponse({"ok": True})

    class _SeedMW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # noqa: ANN001
            return await _seed(request, call_next)

    app = Starlette(
        routes=[Route("/api/v1/sessions", _echo, methods=["GET", "POST"])]
    )
    app.add_middleware(
        APIKeyAuthMiddleware,
        keys_with_labels=keys_with_labels,
        protected_prefix="/api/v1",
    )
    app.add_middleware(DashboardCookieMiddleware)
    app.state.dashboard_internal_keys = dashboard_internal_keys or {}
    app.add_middleware(_SeedMW)
    app.add_middleware(
        SessionMiddleware,
        secret_key=_TEST_SESSION_SECRET,
        session_cookie="gg_relay_session",
        same_site="lax",
    )
    return app


@pytest.mark.asyncio
async def test_actor_equals_api_key_label_post_mutation() -> None:
    """Plain API call with ``X-API-Key`` (operator tag ``"alice"``)
    → ``request.state.api_key_label == "alice"``. The "actor" the
    rest of the stack reads is exactly this field — owner
    attribution, audit log, role lookup all use ``api_key_label``."""
    capture: dict[str, object] = {}
    keys = {"operator-key-abc": "alice"}
    app = _build_full_chain_app(
        keys_with_labels=keys,
        capture=capture,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/sessions",
            headers={"X-API-Key": "operator-key-abc"},
        )
    assert r.status_code == 200
    assert capture["api_key_label"] == "alice"
    # No cookie identity in this path, so dashboard_user stays unset.
    assert capture["dashboard_user"] is None


@pytest.mark.asyncio
async def test_dashboard_cookie_actor_equals_dashboard_user() -> None:
    """Dashboard cookie identity ``alice`` POSTs /api/v1/sessions →
    DashboardCookie injects the internal key bound to label
    ``"dashboard-alice"``; APIKey middleware then resolves
    ``request.state.api_key_label == "dashboard-alice"``. The
    namespaced label (``dashboard-<user>``) matches the D8.22 role
    mapping convention so role lookup is unambiguous."""
    capture: dict[str, object] = {}
    internal_key = "internal-alice-key-xyz"
    # Operator key map + the dashboard-alice synthetic key entry.
    keys = {
        "operator-key-abc": "alice",
        internal_key: "dashboard-alice",
    }
    app = _build_full_chain_app(
        keys_with_labels=keys,
        dashboard_internal_keys={"alice": internal_key},
        capture=capture,
        seed_session={SESSION_KEY: "alice"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post("/api/v1/sessions")
    assert r.status_code == 200
    assert capture["api_key_label"] == "dashboard-alice"
    # Both signals are populated — single contract = api_key_label,
    # but dashboard_user is also available for templates.
    assert capture["dashboard_user"] == "alice"
