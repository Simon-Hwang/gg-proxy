"""Structured request logging middleware.

Emits a JSON-shaped log line per request with method, path, status, latency,
and ``X-Request-Id`` if present. Avoids body capture — bodies can contain
PII; the redaction layer in the persistence path is the right place to
mask, not the access log.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = logging.getLogger("gg_relay.access")

_CallNext = Callable[[Request], Awaitable[Response]]


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    """Emit one structured log line per request."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: _CallNext,
    ) -> Response:
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "request error rid=%s method=%s path=%s duration_ms=%.1f",
                rid,
                request.method,
                request.url.path,
                duration_ms,
            )
            raise
        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "rid=%s method=%s path=%s status=%d duration_ms=%.1f",
            rid,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        response.headers["X-Request-Id"] = rid
        return response
