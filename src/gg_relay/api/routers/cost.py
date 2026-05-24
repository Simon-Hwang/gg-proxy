"""Per-owner cost attribution endpoints — Plan 8 D8.30 / Task 23.

Surface:

  * ``GET  /api/v1/cost/per-owner``    — GROUP BY owner aggregate
                                         (admin sees all; submitter
                                         forced to self).
  * ``GET  /api/v1/cost/per-session``  — per-row breakdown for one
                                         owner (admin may filter by
                                         any owner; submitter forced
                                         to self).
  * ``GET  /api/v1/cost/summary``      — user-centric summary panel
                                         backed by a 30s TTLCache so
                                         dashboard refreshes don't
                                         hammer the store. Admins get
                                         a ``team_total_cost_usd``
                                         field; everyone else sees
                                         ``None``.
  * ``GET  /api/v1/cost/export.csv``   — admin-only CSV export of the
                                         per-owner aggregate. Writes
                                         an ``audit_log`` row with
                                         ``action='cost_export'`` so
                                         the moderation trail captures
                                         every download.

RBAC mirrors the audit router's inline policy (Plan 8 D8.4 / Task 6):

  * ``admin``     — any owner; any window.
  * ``submitter`` — own-owner only. An explicit ``owner=<other>``
                    surfaces as 403 ``forbidden_cost_view`` rather
                    than a silent rewrite — the explicit-deny avoids
                    "I asked for bob's cost and got mine".
  * ``viewer``    — treated as submitter (least-privilege fallthrough).

Why a dedicated 30s TTLCache on ``/summary`` instead of HTTP
``Cache-Control``: the response is per-actor and per-role; an
upstream cache would have to vary by API key which is fragile
across proxy hops. A process-local cache keyed by
``(label, period, is_admin)`` keeps the hit rate high on dashboard
refresh while staying obviously invalidatable (the value is
recomputed every 30 seconds without a manual flush).

CSV export caps at 1000 rows — single-team scale (≤200 owners over
several months of history) easily fits, and the cap defends against
a malformed query producing an unbounded download. Larger windows
should drive ad-hoc Postgres queries instead.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime
from typing import Annotated

from cachetools import TTLCache
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import Response

from gg_relay.api.dependencies.require_role import (
    ROLE_HIERARCHY,
    _resolve_role,
)
from gg_relay.api.schemas import (
    OwnerCostResponse,
    OwnerCostSummary,
    SessionCostBreakdown,
    SessionCostListResponse,
    UserCostSummary,
)

logger = logging.getLogger("gg_relay.api.routers.cost")

router = APIRouter(prefix="/cost", tags=["cost"])


# Process-local TTL cache for ``/summary`` responses.
# Key: ``(label, period, is_admin)`` — admin role affects whether
# ``team_total_cost_usd`` is populated, so role MUST be in the key
# or a submitter promoted to admin would still see the non-team
# variant until the entry expired. ``maxsize=256`` comfortably
# covers a single-team deployment (≤50 dashboard users × 3 periods
# × 2 admin variants); the LRU eviction handles bursts gracefully.
_SUMMARY_CACHE: TTLCache[tuple[str, str, bool], UserCostSummary] = TTLCache(
    maxsize=256, ttl=30
)


def _is_admin(role: str) -> bool:
    """Compare ``role`` against the admin tier of :data:`ROLE_HIERARCHY`.

    Kept as a tiny helper so the four endpoint bodies share the same
    branch shape — a future "moderator" tier between submitter and
    admin only needs to update :data:`ROLE_HIERARCHY` here.
    """
    return ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY["admin"]


@router.get("/per-owner", response_model=OwnerCostResponse)
async def cost_per_owner(
    request: Request,
    owner: Annotated[str | None, Query(max_length=64)] = None,
    from_ts: Annotated[datetime | None, Query()] = None,
    to_ts: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    order_by: Annotated[
        str, Query(pattern=r"^(cost|sessions|owner)$")
    ] = "cost",
) -> OwnerCostResponse:
    """Aggregate cost per owner (admin sees all; submitter forced self).

    Plan 8 D8.30 / Task 23. ``order_by`` controls the secondary
    ordering of the response items:

      * ``cost``     — DESC by ``total_cost_usd`` (default; top
                       spenders first).
      * ``sessions`` — DESC by ``session_count`` (heaviest users
                       by activity).
      * ``owner``    — ASC alphabetical (stable diff-friendly
                       order for CSV consumers).

    The optional ``owner`` filter narrows the GROUP BY result to a
    single row; submitters cannot pass an ``owner`` other than
    their own label (403 ``forbidden_cost_view``).
    """
    store = request.app.state.store
    label = getattr(request.state, "api_key_label", None) or "anon"
    role = _resolve_role(request)
    admin = _is_admin(role)

    if not admin:
        if owner is not None and owner != label:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "cannot view other owners' cost",
                    "code": "forbidden_cost_view",
                    "required_role": "admin",
                    "current_role": role,
                },
            )
        # Force the filter even when the caller omitted it — a
        # submitter must never see another team-mate's aggregate.
        owner = label

    rows = await store.aggregate_cost_by_owner(
        from_ts=from_ts, to_ts=to_ts, limit=limit, order_by=order_by
    )
    # The GROUP BY result naturally contains every owner; apply the
    # owner filter in Python so the SQL stays a single statement.
    # The result set is bounded by ``limit`` (≤200) so the linear
    # scan is cheap.
    if owner is not None:
        rows = [r for r in rows if r.get("owner") == owner]

    items = [
        OwnerCostSummary(
            owner=r.get("owner"),
            session_count=int(r.get("session_count", 0) or 0),
            total_cost_usd=float(r.get("total_cost_usd", 0.0) or 0.0),
        )
        for r in rows
    ]
    return OwnerCostResponse(
        items=items,
        from_ts=from_ts.isoformat() if from_ts else None,
        to_ts=to_ts.isoformat() if to_ts else None,
    )


@router.get("/per-session", response_model=SessionCostListResponse)
async def cost_per_session(
    request: Request,
    owner: Annotated[str | None, Query(max_length=64)] = None,
    from_ts: Annotated[datetime | None, Query()] = None,
    to_ts: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> SessionCostListResponse:
    """List sessions with per-row cost (newest first).

    Plan 8 D8.30 / Task 23. Mirrors :func:`cost_per_owner`'s RBAC:
    submitters are forced to ``owner=<self-label>``. ``next_cursor``
    is always ``None`` in the MVP — operators with large result
    sets should use the CSV export endpoint instead.
    """
    store = request.app.state.store
    label = getattr(request.state, "api_key_label", None) or "anon"
    role = _resolve_role(request)
    admin = _is_admin(role)

    if not admin:
        if owner is not None and owner != label:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "cannot view other owners' sessions",
                    "code": "forbidden_cost_view",
                    "required_role": "admin",
                    "current_role": role,
                },
            )
        owner = label

    rows, next_cursor = await store.list_sessions_with_cost(
        owner=owner, from_ts=from_ts, to_ts=to_ts, limit=limit
    )
    items: list[SessionCostBreakdown] = []
    for r in rows:
        items.append(
            SessionCostBreakdown(
                id=r["id"],
                owner=r.get("owner"),
                status=r["status"],
                submitted_at=r["submitted_at"],
                ended_at=r.get("ended_at"),
                total_cost_usd=float(r.get("cost_usd") or 0.0),
            )
        )
    return SessionCostListResponse(items=items, next_cursor=next_cursor)


@router.get("/summary", response_model=UserCostSummary)
async def cost_summary(
    request: Request,
    period: Annotated[
        str, Query(pattern=r"^(today|this_month|last_30d)$")
    ] = "this_month",
) -> UserCostSummary:
    """User-centric summary with 30s TTL cache.

    Plan 8 D8.30 / Task 23. Cache key ``(label, period, is_admin)``
    — admin role affects whether ``team_total_cost_usd`` is
    populated so it MUST be in the key. A second call within the
    TTL hits the cache without touching the store at all.

    Admin callers get the team-wide total computed from a single
    extra :meth:`aggregate_cost_by_owner` call (limit=1000); for
    larger team sizes consider denormalising into a daily snapshot
    table (future plan).
    """
    store = request.app.state.store
    label = getattr(request.state, "api_key_label", None) or "anon"
    role = _resolve_role(request)
    admin = _is_admin(role)

    cache_key = (label, period, admin)
    cached = _SUMMARY_CACHE.get(cache_key)
    if cached is not None:
        return cached

    own = await store.summary_for_user(user_label=label, period=period)
    team_total: float | None = None
    if admin:
        all_rows = await store.aggregate_cost_by_owner(limit=1000)
        team_total = float(
            sum((r.get("total_cost_usd") or 0.0) for r in all_rows)
        )

    response = UserCostSummary(
        user=label,
        role=role,
        period=period,
        from_ts=own["from_ts"],
        session_count=int(own["session_count"]),
        total_cost_usd=float(own["total_cost_usd"]),
        team_total_cost_usd=team_total,
    )
    _SUMMARY_CACHE[cache_key] = response
    return response


@router.get("/export.csv")
async def cost_export_csv(
    request: Request,
    from_ts: Annotated[datetime | None, Query()] = None,
    to_ts: Annotated[datetime | None, Query()] = None,
) -> Response:
    """CSV export of the per-owner aggregate — admin only.

    Plan 8 D8.30 / Task 23. Writes a single ``audit_log`` row with
    ``action='cost_export'`` so every download is traceable. The
    ``target_id`` carries the requested time window so a follow-up
    audit query can correlate downloads with billing periods.

    Capped at 1000 rows — single-team scale fits easily; larger
    deployments should drive ad-hoc Postgres queries.
    """
    label = getattr(request.state, "api_key_label", None) or "anon"
    role = _resolve_role(request)
    if not _is_admin(role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "admin required for csv export",
                "code": "forbidden_export",
                "required_role": "admin",
                "current_role": role,
            },
        )

    store = request.app.state.store
    audit = getattr(request.app.state, "audit_service", None)
    rows = await store.aggregate_cost_by_owner(
        from_ts=from_ts, to_ts=to_ts, limit=1000
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["owner", "session_count", "total_cost_usd"])
    for r in rows:
        writer.writerow(
            [
                r.get("owner") or "—",
                int(r.get("session_count", 0) or 0),
                float(r.get("total_cost_usd", 0.0) or 0.0),
            ]
        )

    if audit is not None:
        target_id = (
            f"{from_ts.isoformat() if from_ts else '*'}"
            f"_to_{to_ts.isoformat() if to_ts else '*'}"
        )
        try:
            await audit.record(
                actor=label,
                action="cost_export",
                target_type="date_range",
                target_id=target_id,
                metadata={
                    "row_count": len(rows),
                },
                request_id=getattr(request.state, "request_id", None),
            )
        except Exception:  # pragma: no cover — defensive
            logger.exception("audit record_audit failed for cost_export")

    filename = f"cost_export_{datetime.now(UTC).date().isoformat()}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def _clear_summary_cache() -> None:
    """Test-only hook: drop the TTL cache between cases.

    Tests that exercise the cache behaviour (hit / miss across role
    flips) call this before each scenario so a previous test's
    entry doesn't bleed into the next. Production code never calls
    this — the 30s TTL keeps entries fresh on its own.
    """
    _SUMMARY_CACHE.clear()


__all__ = ["router", "_clear_summary_cache"]
