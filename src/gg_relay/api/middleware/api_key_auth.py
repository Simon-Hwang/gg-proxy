"""X-API-Key authentication middleware.

Only paths beginning with ``protected_prefix`` (default ``/api/v1``) are
guarded; dashboard, healthz, and IM webhook endpoints have their own auth
(or are intentionally public). The middleware accepts ANY of the supplied
keys — operators rotate by adding a new key, draining traffic, then
removing the old one.

On a successful auth, ``request.state.api_key_id`` is set to a 16-char
sha256 prefix of the raw key (Plan 7 D7.15 partial). Downstream
middlewares (notably the rate limiter) consume this opaque identifier
without ever seeing the secret. Task 11 will replace the inline hash
with a structured key-id store.
"""
from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

_CallNext = Callable[[Request], Awaitable[Response]]


def _hash_key(key: str) -> str:
    """Return a stable opaque id for an API key without leaking it."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request to ``protected_prefix`` lacking a valid X-API-Key.

    The set is frozen at construction; rotating keys requires a process
    restart (Plan 4 D4.23 — rotation REST API is deferred to v2). When
    ``expected_keys`` is empty *and* ``allow_no_keys`` is true, the
    middleware is a no-op (handy for unit tests that don't care about
    auth wiring).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        expected_keys: Iterable[str] = (),
        protected_prefix: str = "/api/v1",
        allow_no_keys: bool = False,
    ) -> None:
        super().__init__(app)
        self._keys: frozenset[str] = frozenset(expected_keys)
        self._prefix = protected_prefix
        self._allow_no_keys = allow_no_keys

    async def dispatch(
        self,
        request: Request,
        call_next: _CallNext,
    ) -> Response:
        path = request.url.path
        if not path.startswith(self._prefix):
            response = await call_next(request)
            return response
        if self._allow_no_keys and not self._keys:
            response = await call_next(request)
            return response
        header = request.headers.get("X-API-Key")
        if not header or header not in self._keys:
            return JSONResponse(
                {"detail": "invalid_api_key"}, status_code=401
            )
        request.state.api_key_id = _hash_key(header)
        response = await call_next(request)
        return response
