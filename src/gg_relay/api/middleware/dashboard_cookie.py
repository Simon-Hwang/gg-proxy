"""Dashboard cookie → internal API key injection (Plan 8 D8.25 + D8.26).

Browser users authenticate via the username/password form on
``/dashboard/login`` which sets a Starlette SessionMiddleware cookie
carrying ``{"dashboard_user": "<username>"}``. This middleware sits
*outside* :class:`APIKeyAuthMiddleware` in the dispatch chain so that
on every request it can:

1. Read the cookie session and resolve the logged-in ``dashboard_user``
   (no-op if SessionMiddleware hasn't run or the cookie is absent).
2. Expose the username on ``request.state.dashboard_user`` so dashboard
   templates and downstream handlers can branch on the cookie identity
   without re-decoding the cookie themselves.
3. For mutations against ``/api/v1/*`` only, inject a synthetic
   ``X-API-Key`` header bound to an *internal* API key whose label is
   ``dashboard-<username>``. Downstream :class:`APIKeyAuthMiddleware`
   treats the request like any other API caller, so there is exactly
   one ``request.state.api_key_label`` identity (Plan 8 D8.25 — single
   identity contract) feeding owner attribution / role lookup / audit.

Why inject only on ``/api/v1/*``:
  * Non-API paths (``/dashboard/*``, ``/healthz``, ``/metrics``) either
    consume the cookie directly (dashboard templates via
    ``request.state.dashboard_user``) or are public/probes — there is
    no API-key middleware to satisfy.
  * Webhook routes under ``/api/v1/webhooks/`` and ``/im/`` are exempt
    from API-key auth (see ``api_key_auth.WEBHOOK_EXEMPT_PREFIXES``),
    but injecting a header on those paths is still harmless because the
    APIKey middleware short-circuits on the exempt prefix before
    reading any header.

Why replace any existing ``X-API-Key``:
  * Single identity contract (D8.25): when a request carries a valid
    dashboard cookie, the dashboard user is *the* actor. An attacker
    that also sends an ``X-API-Key`` header (e.g. via stolen key) must
    not be able to dual-identify — the cookie identity always wins so
    audit / role-check / owner-attribute all agree.

Internal-key contract (matches D8.22 / D8.28 namespace):
  * Generated at lifespan startup via :func:`secrets.token_urlsafe(32)`
    so each process restart re-rolls the keys (in-memory only —
    operators don't need to rotate anything, the dashboard re-logins
    naturally on the next request).
  * Label format: ``dashboard-<username>``. ``api_keys_with_labels`` is
    pre-seeded with ``{internal_key: "dashboard-<username>"}`` so the
    existing :class:`APIKeyAuthMiddleware` flow validates the synthetic
    header just like any operator-supplied key.
  * Plan 8 v2.3 BLOCKER 1 ramp / D8.29 (Task 22) will optionally swap
    in-memory keys for a DB-backed table so dashboard logins survive
    process restarts; this module's contract is the same either way.

Plan 8 Task 5 will add an Audit middleware between APIKey and
RateLimit; this middleware sits one layer further out (DashboardCookie
is the outermost in the chain) so Audit sees the synthetic header on
``/api/v1/*`` mutations and records ``actor = dashboard-<username>``
without any per-router boilerplate.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = logging.getLogger("gg_relay.api.dashboard_cookie")

_CallNext = Callable[[Request], Awaitable[Response]]

# Session-dict key written by the dashboard login flow. Lives at
# module scope so the dashboard router and the middleware can share
# the same constant without a runtime import cycle.
SESSION_KEY: str = "dashboard_user"

# Path prefix that triggers synthetic ``X-API-Key`` injection. Only
# ``/api/v1/*`` mutations need the downstream APIKey middleware to
# resolve a label; dashboard / health / metrics paths do not.
_API_PREFIX: str = "/api/v1/"


class DashboardCookieMiddleware(BaseHTTPMiddleware):
    """Bind cookie session → internal API key for dashboard mutations.

    Construction (Plan 9 D9.0a):

    * ``cookie_session_key`` — name of the entry in ``request.session``
      (NOT the raw cookie name; the cookie itself is signed and
      managed by Starlette's :class:`SessionMiddleware`). Default
      ``"dashboard_user"``.

    The ``{username: raw_key}`` mapping lives entirely on
    ``app.state.dashboard_internal_keys``. The lifespan populates it
    at startup; Plan 9 D9.10 swaps it for a DB-backed mapping that
    survives process restart. The middleware reads
    ``request.app.state.dashboard_internal_keys`` at dispatch time
    so the lifespan can hot-replace it without rebuilding the
    middleware chain (FastAPI forbids ``add_middleware`` after
    lifespan start).

    Idempotency / safety:

    * Missing ``request.session`` (SessionMiddleware not installed)
      → middleware passes through silently rather than raising.
    * Missing ``app.state.dashboard_internal_keys`` → middleware is
      a no-op (the cookie may still be read for templates, but no
      header is ever injected).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        cookie_session_key: str = SESSION_KEY,
    ) -> None:
        super().__init__(app)
        self._cookie_session_key = cookie_session_key

    @staticmethod
    def _resolve_user_to_key(request: Request) -> Mapping[str, str]:
        """Return the live ``{username: raw_key}`` mapping."""
        state_keys = getattr(
            request.app.state, "dashboard_internal_keys", None
        )
        if isinstance(state_keys, Mapping):
            return state_keys
        return {}

    @staticmethod
    def _safe_session(request: Request) -> dict[str, object]:
        """Return ``request.session`` or an empty dict if unavailable.

        ``request.session`` is a property that raises ``AssertionError``
        when :class:`SessionMiddleware` hasn't run; ``getattr`` does
        NOT catch ``AssertionError`` (it only catches
        ``AttributeError``), so we have to try/except explicitly.
        """
        try:
            session = request.session
        except (AssertionError, AttributeError):
            return {}
        if not isinstance(session, dict):
            return {}
        return session

    async def dispatch(
        self,
        request: Request,
        call_next: _CallNext,
    ) -> Response:
        session = self._safe_session(request)
        username_raw = session.get(self._cookie_session_key)
        username = (
            username_raw if isinstance(username_raw, str) else None
        )
        user_to_key = self._resolve_user_to_key(request)
        if username and username in user_to_key:
            request.state.dashboard_user = username
            path = request.url.path
            if path.startswith(_API_PREFIX):
                self._inject_synthetic_key(request, username, user_to_key)
        return await call_next(request)

    def _inject_synthetic_key(
        self,
        request: Request,
        username: str,
        user_to_key: Mapping[str, str],
    ) -> None:
        """Rewrite ``request.scope["headers"]`` to carry the synthetic
        ``X-API-Key`` for ``username``.

        Any pre-existing ``X-API-Key`` header is dropped — the cookie
        identity wins (single identity contract, D8.25). Header lookup
        is case-insensitive per HTTP, which is why we lowercase before
        comparing the bytes form of the header name.
        """
        key = user_to_key[username]
        headers: list[tuple[bytes, bytes]] = [
            (k, v)
            for k, v in request.scope.get("headers", [])
            if k.lower() != b"x-api-key"
        ]
        headers.append((b"x-api-key", key.encode("utf-8")))
        request.scope["headers"] = headers
