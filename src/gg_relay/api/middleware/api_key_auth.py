"""X-API-Key authentication middleware.

Only paths beginning with ``protected_prefix`` (default ``/api/v1``) are
guarded; dashboard, healthz, and IM webhook endpoints have their own auth
(or are intentionally public). The middleware accepts ANY of the supplied
keys — operators rotate by adding a new key, draining traffic, then
removing the old one.

On a successful auth, two ``request.state`` fields are populated:

* ``request.state.api_key_id``    — 16-char sha256 prefix of the raw
  key (Plan 7 D7.15 partial — rate-limit + log-redaction identifier).
* ``request.state.api_key_label`` — Plan 7 D7.26 — operator-supplied
  human-readable label from ``RELAY_API_KEYS_RAW`` (``key:label`` /
  ``label=key`` token shapes). Legacy bare keys auto-derive a
  ``key-<sha256[:8]>`` label so existing callers always have one.
  The sessions router reads this and writes it to ``sessions.owner``
  whenever the client doesn't pass an explicit ``owner`` in the
  request body (auto-attribute owner).

Plan 7 Task 6b — the constructor signature is ``keys_with_labels:
Mapping[str, str]`` (was ``expected_keys: Iterable[str]``). This is a
deliberate breaking change to the call site — :func:`create_app`
passes ``cfg.api_keys_with_labels`` and tests construct a
``{key: label}`` dict directly.
"""
from __future__ import annotations

import hashlib
import secrets as stdlib_secrets
from collections.abc import Awaitable, Callable, Mapping

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

    The labelled-key map is frozen at construction; rotating keys
    requires a process restart (Plan 4 D4.23 — rotation REST API is
    deferred to v2). When ``keys_with_labels`` is empty *and*
    ``allow_no_keys`` is true, the middleware is a no-op (handy for
    unit tests that don't care about auth wiring).

    Key comparison uses :func:`secrets.compare_digest` to keep the
    timing-side-channel surface tight.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        keys_with_labels: Mapping[str, str],
        protected_prefix: str = "/api/v1",
        allow_no_keys: bool = False,
    ) -> None:
        super().__init__(app)
        # Defensive copy so a caller mutating the source dict after
        # construction can't sneak in new keys past startup.
        self._keys_with_labels: dict[str, str] = dict(keys_with_labels)
        self._prefix = protected_prefix
        self._allow_no_keys = allow_no_keys

    async def dispatch(
        self,
        request: Request,
        call_next: _CallNext,
    ) -> Response:
        path = request.url.path
        if not path.startswith(self._prefix):
            return await call_next(request)
        if self._allow_no_keys and not self._keys_with_labels:
            return await call_next(request)
        header = request.headers.get("X-API-Key")
        if not header:
            return JSONResponse(
                {"detail": "invalid_api_key"}, status_code=401
            )
        for k, label in self._keys_with_labels.items():
            if stdlib_secrets.compare_digest(header, k):
                request.state.api_key_id = _hash_key(k)
                request.state.api_key_label = label
                return await call_next(request)
        return JSONResponse(
            {"detail": "invalid_api_key"}, status_code=401
        )
