"""X-API-Key authentication middleware.

Only paths beginning with ``protected_prefix`` (default ``/api/v1``) are
guarded; dashboard, healthz, and IM webhook endpoints have their own auth
(or are intentionally public). The middleware accepts ANY of the supplied
keys — operators rotate by adding a new key, draining traffic, then
removing the old one.

On a successful auth, three ``request.state`` fields are populated:

* ``request.state.api_key_id``    — 16-char sha256 prefix of the raw
  key (Plan 7 D7.15 partial — rate-limit + log-redaction identifier).
* ``request.state.api_key_label`` — Plan 7 D7.26 — operator-supplied
  human-readable label from ``RELAY_API_KEYS_RAW`` (``key:label`` /
  ``label=key`` token shapes). Legacy bare keys auto-derive a
  ``key-<sha256[:8]>`` label so existing callers always have one.
  The sessions router reads this and writes it to ``sessions.owner``
  whenever the client doesn't pass an explicit ``owner`` in the
  request body (auto-attribute owner).
* ``request.state.api_key_hash``  — Plan 8 D8.29 — full 64-char
  sha256 hex digest of the raw key. Surfaced for the audit fallback
  middleware so it can correlate fallback rows with the
  ``api_keys.key_hash`` column without re-hashing.

Plan 8 Task 22 (D8.29) — TWO resolution paths coexist:

* **New path (preferred)** — :class:`gg_relay.auth.protocol.KeyResolver`
  attached to ``request.app.state.key_resolver``. Async lookup with
  TTL cache; backs the dashboard ``/admin/keys`` self-service flow.
* **Legacy path (Plan 7 compat)** — ``keys_with_labels`` frozen dict
  passed at construction. Still wired by tests that don't seed a
  resolver. The middleware probes ``app.state.key_resolver`` first
  on every request; only falls back to the frozen dict when no
  resolver is attached.

Both paths populate the same ``request.state`` fields so downstream
consumers (require_role, owner attribution, audit fallback) don't
care which one matched.

Plan 7 Task 11 (D7.15) — webhook routes under ``/api/v1/webhooks/`` and
the deprecated ``/im/`` alias are EXEMPT from API-key auth: IM
providers (Feishu, DingTalk, Slack, …) cannot send ``X-API-Key`` on
inbound callbacks, so those paths rely on their own signature
verification (see :func:`gg_relay.im.backends.feishu.verify_feishu_signature`
and :func:`gg_relay.im.router._process_feishu_callback`).
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


# Plan 7 Task 11 (D7.15) — webhook routes that handle their own
# signature verification and must NOT require an X-API-Key. Listed
# as a module-level constant so tests can introspect / assert the
# coupling with Task 12's webhook router.
WEBHOOK_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/v1/webhooks/",  # canonical Feishu / future IM callbacks
    "/im/",  # deprecated alias kept for the 0.7 → 0.8 migration
)


def _hash_key(key: str) -> str:
    """Return a stable 16-char opaque id for an API key without leaking it."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _hash_key_full(key: str) -> str:
    """Return the full 64-char sha256 hex digest of ``key``.

    Plan 8 D8.29 — populates ``request.state.api_key_hash`` so the
    audit fallback middleware can correlate with ``api_keys.key_hash``
    without re-hashing.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request to ``protected_prefix`` lacking a valid X-API-Key.

    Two resolution paths (Plan 7 → Plan 8 transition):

      * **Resolver path** — if ``request.app.state.key_resolver`` is
        set (Plan 8 Task 22), the middleware awaits
        :meth:`KeyResolver.resolve` for every request. The resolver
        owns its own cache + revocation logic; we just translate
        a ``None`` return into a 401.
      * **Frozen-dict path** — fallback when no resolver is attached.
        Compares the raw header against the construction-time
        ``keys_with_labels`` dict with :func:`secrets.compare_digest`
        (Plan 7 D7.15 timing-safe). When the dict is empty AND
        ``allow_no_keys=True``, the middleware is a no-op (handy
        for unit tests that don't care about auth wiring).

    Webhook routes listed in :data:`WEBHOOK_EXEMPT_PREFIXES` bypass
    auth so IM providers (Feishu, …) can hit the canonical
    ``/api/v1/webhooks/*`` paths without an ``X-API-Key`` header.
    The webhook router uses HMAC signature verification instead
    (Plan 7 Task 12 / D7.16).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        keys_with_labels: Mapping[str, str] | None = None,
        protected_prefix: str = "/api/v1",
        allow_no_keys: bool = False,
    ) -> None:
        super().__init__(app)
        # Defensive copy so a caller mutating the source dict after
        # construction can't sneak in new keys past startup.
        self._keys_with_labels: dict[str, str] = dict(keys_with_labels or {})
        self._prefix = protected_prefix
        self._allow_no_keys = allow_no_keys

    async def dispatch(
        self,
        request: Request,
        call_next: _CallNext,
    ) -> Response:
        path = request.url.path
        # Webhook paths use their own signature verification (Plan 7
        # Task 11 / D7.15 — closes the Task 12 coupling note).
        if any(path.startswith(pre) for pre in WEBHOOK_EXEMPT_PREFIXES):
            return await call_next(request)
        if not path.startswith(self._prefix):
            return await call_next(request)
        header = request.headers.get("X-API-Key")
        # Resolver path takes precedence when present (Plan 8 D8.29).
        resolver = getattr(
            getattr(request.app, "state", None), "key_resolver", None
        )
        if resolver is not None:
            if not header:
                return JSONResponse(
                    {"detail": "invalid_api_key"}, status_code=401
                )
            resolved = await resolver.resolve(header)
            if resolved is None:
                return JSONResponse(
                    {"detail": "invalid_api_key"}, status_code=401
                )
            request.state.api_key_id = _hash_key(header)
            request.state.api_key_hash = _hash_key_full(header)
            request.state.api_key_label = resolved.label
            request.state.api_key_role = resolved.role
            return await call_next(request)
        # Legacy frozen-dict path (Plan 7 compat — tests + bootstrap
        # phase before the resolver is wired into app.state).
        if self._allow_no_keys and not self._keys_with_labels:
            return await call_next(request)
        if not header:
            return JSONResponse(
                {"detail": "invalid_api_key"}, status_code=401
            )
        for k, label in self._keys_with_labels.items():
            if stdlib_secrets.compare_digest(header, k):
                request.state.api_key_id = _hash_key(k)
                request.state.api_key_hash = _hash_key_full(k)
                request.state.api_key_label = label
                return await call_next(request)
        return JSONResponse(
            {"detail": "invalid_api_key"}, status_code=401
        )
