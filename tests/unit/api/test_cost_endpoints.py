"""Unit tests for ``/api/v1/cost/*`` endpoints — Plan 8 D8.30 / Task 23.

Four black-box tests driving a minimal FastAPI app that mounts the
cost router alongside a fake store + audit service so the focus stays
on RBAC, the TTL cache, and the CSV export branch without dragging in
the full lifespan stack.

Tests:

  * ``test_per_owner_admin_sees_all`` — admin GET returns every
    owner row the fake store reports.
  * ``test_per_owner_submitter_forced_self_403_other`` — non-admin
    asking for someone else's owner → 403 ``forbidden_cost_view``;
    no ``owner=`` arg is silently rewritten to self.
  * ``test_summary_cache_30s_no_double_query`` — calling
    ``GET /summary`` twice with the same actor / period hits the
    store EXACTLY ONCE; the cached response satisfies the second
    call.
  * ``test_export_csv_admin_only_403_for_submitter`` — submitter
    CSV download → 403 ``forbidden_export``; no audit row written.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from starlette.middleware.base import BaseHTTPMiddleware

from gg_relay.api.routers.cost import _clear_summary_cache, router as cost_router

pytestmark = pytest.mark.asyncio


class _FakeStore:
    """Minimal store stand-in tracking call counts so the cache
    test can prove the TTLCache short-circuits the second call.

    Returns canned aggregate rows / per-user summaries — the cost
    router does no SQL of its own so a Python dict response is
    indistinguishable from a real RowMapping.
    """

    def __init__(self) -> None:
        self.aggregate_calls = 0
        self.summary_calls = 0
        self.list_calls = 0

    async def aggregate_cost_by_owner(
        self,
        *,
        from_ts: Any = None,
        to_ts: Any = None,
        limit: int = 50,
        order_by: str = "cost",
    ) -> list[dict[str, Any]]:
        del from_ts, to_ts, limit, order_by
        self.aggregate_calls += 1
        return [
            {"owner": "alice", "session_count": 3, "total_cost_usd": 1.5},
            {"owner": "bob", "session_count": 1, "total_cost_usd": 10.0},
        ]

    async def list_sessions_with_cost(
        self,
        *,
        owner: str | None = None,
        from_ts: Any = None,
        to_ts: Any = None,
        after: str | None = None,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], str | None]:
        del from_ts, to_ts, after, limit
        self.list_calls += 1
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        rows = [
            {
                "id": "sid-A",
                "owner": owner or "alice",
                "status": "completed",
                "submitted_at": now,
                "ended_at": now,
                "cost_usd": 0.25,
            }
        ]
        return rows, None

    async def summary_for_user(
        self, *, user_label: str, period: str = "this_month"
    ) -> dict[str, Any]:
        self.summary_calls += 1
        from datetime import UTC, datetime

        return {
            "user": user_label,
            "period": period,
            "from_ts": datetime.now(UTC).isoformat(),
            "session_count": 2,
            "total_cost_usd": 0.5,
        }


class _RecordingAudit:
    """Captures every ``record(...)`` call as a dict.

    The CSV export test asserts that the admin call writes one row
    and the 403 submitter call writes zero — exercising the
    audit-on-success contract end-to-end.
    """

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> int:
        self.records.append(kwargs)
        return len(self.records)


class _FakeIdentityMiddleware(BaseHTTPMiddleware):
    """Injects ``api_key_label`` from an ``X-Test-Label`` header.

    The real :class:`APIKeyAuthMiddleware` plus role_mapping wiring
    is overkill for a unit test — this middleware lets each test
    pick its own identity without booting the lifespan.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        label = request.headers.get("X-Test-Label")
        if label:
            request.state.api_key_label = label
        return await call_next(request)


def _make_app(
    *,
    store: _FakeStore,
    audit: _RecordingAudit | None = None,
    role_mapping: dict[str, str] | None = None,
) -> FastAPI:
    """Build a tiny FastAPI app with the cost router + fake state.

    The autouse conftest patch grants ``admin`` when
    ``cfg.role_mapping`` is empty AND a label is present; tests
    that want strict RBAC pass an explicit ``role_mapping`` to
    bypass the patch.
    """
    app = FastAPI()

    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.role_mapping = role_mapping or {}
    app.state.config = cfg
    app.state.store = store
    if audit is not None:
        app.state.audit_service = audit
    app.add_middleware(_FakeIdentityMiddleware)
    app.include_router(cost_router, prefix="/api/v1")
    return app


@pytest_asyncio.fixture(autouse=True)
async def _reset_cache() -> AsyncIterator[None]:
    _clear_summary_cache()
    yield
    _clear_summary_cache()


async def test_per_owner_admin_sees_all() -> None:
    """Admin GET returns every owner row the store reports.

    Role mapping marks ``alice=admin`` so the production resolver
    sees an admin (the conftest patch only kicks in when
    role_mapping is empty).
    """
    store = _FakeStore()
    app = _make_app(store=store, role_mapping={"alice": "admin"})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(
            "/api/v1/cost/per-owner",
            headers={"X-Test-Label": "alice"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    owners = {it["owner"] for it in body["items"]}
    assert owners == {"alice", "bob"}, body
    assert any(it["total_cost_usd"] == 10.0 for it in body["items"])


async def test_per_owner_submitter_forced_self_403_other() -> None:
    """Non-admin asking for someone else's owner → 403."""
    store = _FakeStore()
    app = _make_app(
        store=store,
        role_mapping={"alice": "submitter", "bob": "admin"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(
            "/api/v1/cost/per-owner?owner=bob",
            headers={"X-Test-Label": "alice"},
        )
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "forbidden_cost_view"
    assert detail["required_role"] == "admin"
    assert detail["current_role"] == "submitter"


async def test_summary_cache_30s_no_double_query() -> None:
    """Two ``GET /summary`` calls with the same actor + period →
    store.summary_for_user is called EXACTLY ONCE.

    Proves the TTLCache (key = ``(label, period, is_admin)``)
    short-circuits the second call before it reaches the store.
    The autouse ``_reset_cache`` fixture ensures the cache is
    cold at the start of every test.
    """
    store = _FakeStore()
    app = _make_app(store=store, role_mapping={"alice": "submitter"})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r1 = await c.get(
            "/api/v1/cost/summary",
            headers={"X-Test-Label": "alice"},
        )
        r2 = await c.get(
            "/api/v1/cost/summary",
            headers={"X-Test-Label": "alice"},
        )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json() == r2.json()
    assert store.summary_calls == 1, (
        f"summary_for_user called {store.summary_calls} times; "
        "TTL cache did not absorb the second request"
    )


async def test_export_csv_admin_only_403_for_submitter() -> None:
    """Submitter CSV download → 403; no audit row written.

    The audit-recording fake captures every ``record(...)`` call so
    we can assert the 403 branch never even reaches the audit
    write (the policy gate is in front of the audit line).
    """
    store = _FakeStore()
    audit = _RecordingAudit()
    app = _make_app(
        store=store,
        audit=audit,
        role_mapping={"alice": "submitter", "bob": "admin"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r_forbid = await c.get(
            "/api/v1/cost/export.csv",
            headers={"X-Test-Label": "alice"},
        )
        # Admin succeeds and writes one audit row.
        r_ok = await c.get(
            "/api/v1/cost/export.csv",
            headers={"X-Test-Label": "bob"},
        )
    assert r_forbid.status_code == 403, r_forbid.text
    detail = r_forbid.json()["detail"]
    assert detail["code"] == "forbidden_export"
    assert detail["current_role"] == "submitter"

    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.headers["content-type"].startswith("text/csv")
    # First CSV row is the header; check the column layout.
    csv_body = r_ok.text
    assert csv_body.splitlines()[0] == "owner,session_count,total_cost_usd"

    # Audit captured the admin's download but NOT the submitter's
    # forbidden attempt — the policy gate runs before the audit
    # write.
    assert len(audit.records) == 1
    rec = audit.records[0]
    assert rec["actor"] == "bob"
    assert rec["action"] == "cost_export"
    assert rec["target_type"] == "date_range"
