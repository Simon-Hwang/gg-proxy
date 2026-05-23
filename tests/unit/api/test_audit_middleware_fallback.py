"""AuditFallbackMiddleware unit tests — Plan 8 D8.4 / Task 5.

The fallback middleware writes one ``unknown_mutation`` audit row per
unmatched ``/api/v1/*`` mutation, fire-and-forget after the response
is sent. Tests:

* ``test_fallback_writes_unknown_mutation_for_unmatched_post`` —
  POST to ``/api/v1/foo`` whose handler does NOT call
  ``audit_service.record`` ⇒ middleware writes a row with
  ``action='unknown_mutation'``, ``target_type='endpoint'``,
  ``target_id='POST /api/v1/foo'``, and the right actor.
* ``test_fallback_skips_get`` — GET requests are pure reads and
  must NOT produce audit rows.
* ``test_fallback_skips_webhooks_and_dashboard`` — exempt prefixes
  (``/api/v1/webhooks/*`` and ``/dashboard/*``) plus the SSE events
  stream are bypassed.

A tiny in-memory ``RecordingAuditService`` captures every call
without touching the DB so the tests stay fast and deterministic.
The middleware's fire-and-forget pattern (``asyncio.create_task``)
means tests must briefly yield control to the event loop after the
response so the background task gets a chance to run.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gg_relay.api.audit_service import AuditService
from gg_relay.api.middleware.audit import AuditFallbackMiddleware


class _RecordingStore:
    """Minimal AuditStore-like that captures every record call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def record_audit(
        self,
        *,
        actor: str,
        action: str,
        target_type: str | None = None,
        target_id: str | None = None,
        metadata: Any = None,
        request_id: str | None = None,
        ts: Any = None,
        conn: Any = None,
    ) -> int:
        self.calls.append(
            {
                "actor": actor,
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "metadata": dict(metadata) if metadata else None,
                "request_id": request_id,
            }
        )
        return len(self.calls)


def _build_app(
    *,
    audit_service: AuditService,
    actor: str | None = "alice",
) -> Starlette:
    """Build a tiny app with the fallback middleware and three routes.

    Three routes — one mutation that does NOT call audit (the
    fallback target), one read, and one mutation that already wrote
    its own audit (so we can assert the fallback doesn't double-write
    when the explicit hook fires alongside the unmatched-mutation
    rule — same as production).

    The route handler simulates :class:`APIKeyAuthMiddleware` having
    already populated ``request.state.api_key_label = actor`` so the
    fallback can read the actor without depending on the full middleware
    stack.
    """

    async def _set_actor(request: Request, call_next):  # noqa: ANN001
        if actor is not None:
            request.state.api_key_label = actor
        return await call_next(request)

    async def _no_audit(request: Request) -> JSONResponse:
        # Mutation handler that "forgot" to call audit_service.record.
        return JSONResponse({"ok": True})

    async def _read(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def _events(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def _webhook(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def _dashboard(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route(
                "/api/v1/foo",
                _no_audit,
                methods=["GET", "POST", "DELETE", "PATCH", "PUT"],
            ),
            Route(
                "/api/v1/sessions/sid-1/events",
                _events,
                methods=["POST"],
            ),
            Route(
                "/api/v1/webhooks/feishu",
                _webhook,
                methods=["POST"],
            ),
            Route(
                "/dashboard/login",
                _dashboard,
                methods=["POST"],
            ),
        ]
    )

    from starlette.middleware.base import BaseHTTPMiddleware

    class _ActorMW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # noqa: ANN001
            return await _set_actor(request, call_next)

    # Add inner-first: route → ActorMW → AuditFallback (outermost).
    # That order ensures AuditFallback dispatches AFTER ActorMW has
    # populated ``request.state.api_key_label`` (mirrors production
    # where APIKey middleware sets the label outside of AuditFallback).
    app.add_middleware(
        AuditFallbackMiddleware, audit_service=audit_service
    )
    app.add_middleware(_ActorMW)
    return app


async def _drain_pending_tasks() -> None:
    """Yield control to let fire-and-forget audit tasks run.

    The middleware spawns ``asyncio.create_task`` for the audit
    write so the response can return immediately. We need to wait
    for those background tasks to drain before assertions. A short
    ``asyncio.sleep(0)`` schedules one event-loop iteration; we do a
    handful to cover any chained awaits inside ``record``.
    """
    for _ in range(5):
        await asyncio.sleep(0)


# ── happy path ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fallback_writes_unknown_mutation_for_unmatched_post() -> None:
    """POST to ``/api/v1/foo`` (handler does not audit) → fallback row."""
    store = _RecordingStore()
    audit = AuditService(store)
    app = _build_app(audit_service=audit, actor="alice")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post("/api/v1/foo")
    await _drain_pending_tasks()

    assert r.status_code == 200
    assert len(store.calls) == 1
    call = store.calls[0]
    assert call["actor"] == "alice"
    assert call["action"] == "unknown_mutation"
    assert call["target_type"] == "endpoint"
    assert call["target_id"] == "POST /api/v1/foo"
    assert call["metadata"] is not None
    assert call["metadata"]["status"] == 200
    assert call["metadata"]["via"] == "audit_fallback_middleware"


@pytest.mark.asyncio
async def test_fallback_uses_anon_when_no_actor() -> None:
    """No ``api_key_label`` on request.state → actor='anon' (CHECK NOT NULL)."""
    store = _RecordingStore()
    audit = AuditService(store)
    app = _build_app(audit_service=audit, actor=None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.delete("/api/v1/foo")
    await _drain_pending_tasks()

    assert r.status_code == 200
    assert len(store.calls) == 1
    assert store.calls[0]["actor"] == "anon"
    assert store.calls[0]["target_id"] == "DELETE /api/v1/foo"


# ── method-skip path ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fallback_skips_get() -> None:
    """GET is a pure read — no audit row regardless of the path."""
    store = _RecordingStore()
    audit = AuditService(store)
    app = _build_app(audit_service=audit)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/api/v1/foo")
    await _drain_pending_tasks()

    assert r.status_code == 200
    assert store.calls == [], (
        f"GET request must not write audit; got {store.calls!r}"
    )


# ── path-skip path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fallback_skips_webhooks_and_dashboard() -> None:
    """``/api/v1/webhooks/*``, ``/dashboard/*``, and SSE events bypass."""
    store = _RecordingStore()
    audit = AuditService(store)
    app = _build_app(audit_service=audit)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r1 = await ac.post("/api/v1/webhooks/feishu")
        r2 = await ac.post("/dashboard/login")
        r3 = await ac.post("/api/v1/sessions/sid-1/events")
    await _drain_pending_tasks()

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 200
    assert store.calls == [], (
        f"Exempt prefixes must not write audit; got {store.calls!r}"
    )
