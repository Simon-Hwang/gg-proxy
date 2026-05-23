"""Fallback audit middleware — Plan 8 D8.4 (Task 5).

Best-effort safety net for ``/api/v1/*`` mutations whose handler did
NOT call :meth:`gg_relay.api.audit_service.AuditService.record`
inline. Writes one ``action='unknown_mutation'`` row per such request
*after* the response is sent, fire-and-forget, so:

* the response latency is not affected by the audit write,
* SSE / streaming responses are not held open by an audit transaction,
* a failing audit DB does not surface as a 500 to the caller.

v2.1 MAJOR 3 contract: this middleware is a FALLBACK ONLY. Sensitive
mutations MUST call :meth:`AuditService.record` inline (and ideally
pass ``conn=`` for the durable-outbox same-tx pattern). Inline writes
are observable, atomic with the business mutation, and produce
meaningful ``action`` strings (``session_create`` / ``session_cancel``
/ …). The fallback's ``unknown_mutation`` rows are an
operator-visible signal — "the handler at ``METHOD path`` is missing
its audit hook" — not a substitute for proper instrumentation.

Path filtering:
* ``AUDIT_METHODS`` — only mutation verbs are audited (GET / HEAD /
  OPTIONS are pure reads).
* ``EXEMPT_PATH_PREFIXES`` — webhook callbacks (no operator actor),
  dashboard pages (cookie identity already audited at the API layer
  the dashboard hits next), and SSE event subscriptions
  (long-running, never a mutation).
* The middleware short-circuits non-``/api/v1/*`` paths so dashboard
  POSTs (``/dashboard/login``) and health probes never produce audit
  rows.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from gg_relay.api.audit_service import AuditService

logger = logging.getLogger("gg_relay.api.audit_fallback")

_CallNext = Callable[[Request], Awaitable[Response]]

# HTTP verbs treated as mutations. Anything else (GET / HEAD /
# OPTIONS / TRACE) is a pure read and never audited. Kept as a frozen
# set so test code can introspect the contract without copying.
AUDIT_METHODS: frozenset[str] = frozenset({"POST", "DELETE", "PATCH", "PUT"})

# Path prefixes that bypass fallback audit. Webhooks have no operator
# actor (signature-verified bot callback); dashboard renders a UI and
# uses the cookie identity at the API layer it hits next; SSE event
# subscriptions are long-running reads that do not mutate state. The
# regex-style ``/sessions/.*/events`` shape is matched as a prefix
# match against ``"/api/v1/sessions/"`` plus a per-call ``/events``
# suffix probe so we don't pull in the ``re`` module.
_EXEMPT_PATH_PREFIXES: tuple[str, ...] = (
    "/api/v1/webhooks/",
    "/dashboard/",
    "/im/",
)
_SSE_EVENTS_PREFIX: str = "/api/v1/sessions/"
_SSE_EVENTS_SUFFIX: str = "/events"

# Path prefix that the middleware actually audits. Outside this prefix
# (e.g. ``/dashboard/*``, ``/healthz``) the fallback is a no-op.
_API_PREFIX: str = "/api/v1/"


def _is_exempt(path: str) -> bool:
    """True iff ``path`` is in the explicit exempt list.

    Three branches:
      * Plain prefix match against :data:`_EXEMPT_PATH_PREFIXES`.
      * SSE per-session events stream:
        ``/api/v1/sessions/<sid>/events`` — long-running read that
        cannot be a mutation.
    """
    if any(path.startswith(pre) for pre in _EXEMPT_PATH_PREFIXES):
        return True
    if (
        path.startswith(_SSE_EVENTS_PREFIX)
        and path.endswith(_SSE_EVENTS_SUFFIX)
    ):
        return True
    return False


class AuditFallbackMiddleware(BaseHTTPMiddleware):
    """Fire-and-forget audit fallback for unmatched API mutations.

    Construction:
      * ``audit_service`` — the canonical
        :class:`gg_relay.api.audit_service.AuditService` shared across
        the app. Required (passing ``None`` would silently disable
        every fallback write — better to fail loud at construction).

    Dispatch:
      1. Forward the request, capture the response.
      2. If the request method is in :data:`AUDIT_METHODS` AND the
         path starts with ``/api/v1/`` AND the path is not exempt:
         spawn a background task to call
         :meth:`AuditService.record`. The task is detached
         (``asyncio.create_task``) so the response returns immediately.
      3. Always return the original response unchanged.

    Failures inside the background task are caught and logged at WARN
    so a flaky DB connection cannot cascade into per-request errors.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        audit_service: AuditService | None = None,
    ) -> None:
        super().__init__(app)
        # ``audit_service`` may be ``None`` at construction time —
        # ``create_app`` adds the middleware before the lifespan that
        # builds the engine + store + AuditService runs. In that case
        # the middleware reads ``request.app.state.audit_service`` at
        # dispatch time. Tests pass an explicit instance so they don't
        # need the FastAPI lifespan running.
        self._audit: AuditService | None = audit_service

    def _resolve_service(
        self, request: Request
    ) -> AuditService | None:
        """Return the audit service to use for this request.

        Construction-time injection wins; otherwise we look up
        ``request.app.state.audit_service`` (set by the FastAPI
        lifespan). Returns ``None`` if neither is available — the
        caller skips the fallback in that case (no service ⇒ no
        audit, but the response is unaffected).
        """
        if self._audit is not None:
            return self._audit
        try:
            service = getattr(request.app.state, "audit_service", None)
        except Exception:
            return None
        return service if isinstance(service, AuditService) else None

    async def dispatch(
        self,
        request: Request,
        call_next: _CallNext,
    ) -> Response:
        response = await call_next(request)
        path = request.url.path
        if (
            request.method in AUDIT_METHODS
            and path.startswith(_API_PREFIX)
            and not _is_exempt(path)
        ):
            service = self._resolve_service(request)
            if service is None:
                return response
            actor = (
                getattr(request.state, "api_key_label", None)
                or "anon"
            )
            request_id = (
                response.headers.get("X-Request-Id")
                or getattr(request.state, "request_id", None)
            )
            asyncio.create_task(
                self._safe_record(
                    service=service,
                    actor=actor,
                    method=request.method,
                    path=path,
                    status=response.status_code,
                    request_id=request_id,
                ),
                name=f"audit-fallback-{request.method}-{path}",
            )
        return response

    async def _safe_record(
        self,
        *,
        service: AuditService,
        actor: str,
        method: str,
        path: str,
        status: int,
        request_id: str | None,
    ) -> None:
        """Record one ``unknown_mutation`` audit row, swallowing errors.

        The fallback is fire-and-forget by design: we never want a
        flaky audit DB to cascade into a per-request error path that
        the caller already finished observing.
        """
        try:
            await service.record(
                actor=actor,
                action="unknown_mutation",
                target_type="endpoint",
                target_id=f"{method} {path}",
                metadata={
                    "status": int(status),
                    "via": "audit_fallback_middleware",
                },
                request_id=request_id,
            )
        except Exception:
            logger.warning(
                "audit fallback failed for %s %s (status=%d, actor=%s)",
                method,
                path,
                status,
                actor,
                exc_info=True,
            )


__all__ = [
    "AUDIT_METHODS",
    "AuditFallbackMiddleware",
]
