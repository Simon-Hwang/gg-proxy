"""Plan 9 D9.12 — admin drain endpoint tests.

Covers:

1. POST /admin/drain sets app.state.drained=True + emits
   drain_started_at timestamp.
2. /readyz returns 503 "drained" after drain.
3. DELETE /admin/drain cancels (idempotent restoration).
4. POST is idempotent (second call returns same timestamp).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from gg_relay.api.dependencies.require_role import require_role
from gg_relay.api.routers.admin_drain import router as drain_router
from gg_relay.api.routers.health import router as health_router


def _build_app() -> FastAPI:
    """Minimal FastAPI app with the drain + health routers.

    Overrides :func:`require_role("admin")` to a no-op so the
    endpoint is callable without the full auth chain — that's
    orthogonal to the drain semantics being tested.
    """
    app = FastAPI()
    app.include_router(drain_router, prefix="/api/v1")
    app.include_router(health_router)

    async def _allow_all() -> None:
        return None

    # ``require_role`` is a *factory* — each route gets its own
    # callable instance at import time, so we walk every dep on
    # the drain routes and override them all.
    overridden = 0
    for route in app.routes:
        if not getattr(route, "path", "").endswith("/admin/drain"):
            continue
        for dep in getattr(getattr(route, "dependant", None), "dependencies", []):
            app.dependency_overrides[dep.call] = _allow_all
            overridden += 1
    assert overridden >= 2, "expected to override the auth dep on both POST + DELETE"
    # Also catch any callers that resolve the factory again
    _ = require_role  # keep import alive

    # Stub minimal app.state so /readyz doesn't 503 with "starting"
    class _StubManager:
        accepting_new = True

    app.state.manager = _StubManager()
    return app


@pytest.mark.asyncio
async def test_post_drain_flips_state() -> None:
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post("/api/v1/admin/drain")
    assert r.status_code == 200
    body = r.json()
    assert body["drained"] is True
    assert "drain_started_at" in body
    assert app.state.drained is True


@pytest.mark.asyncio
async def test_readyz_returns_503_after_drain() -> None:
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r1 = await ac.get("/readyz")
        assert r1.status_code == 200
        await ac.post("/api/v1/admin/drain")
        r2 = await ac.get("/readyz")
    assert r2.status_code == 503
    assert r2.json()["detail"] == "drained"


@pytest.mark.asyncio
async def test_post_drain_idempotent() -> None:
    """Second POST keeps the original drain_started_at."""
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r1 = await ac.post("/api/v1/admin/drain")
        r2 = await ac.post("/api/v1/admin/drain")
    assert r1.json()["drain_started_at"] == r2.json()["drain_started_at"]


@pytest.mark.asyncio
async def test_delete_drain_cancels() -> None:
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        await ac.post("/api/v1/admin/drain")
        assert app.state.drained is True
        r = await ac.delete("/api/v1/admin/drain")
    assert r.status_code == 200
    assert r.json()["drained"] is False
    assert app.state.drained is False


@pytest.mark.asyncio
async def test_readyz_recovers_after_undrain() -> None:
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        await ac.post("/api/v1/admin/drain")
        await ac.delete("/api/v1/admin/drain")
        r = await ac.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"
