"""HTMX dashboard router.

Authentication paths (Plan 4 D4.11 + Plan 8 D8.26):

* **Legacy admin** — single shared admin account; password is loaded
  from ``Config.dashboard_admin_password`` and compared with
  :func:`secrets.compare_digest`. Username is always ``"admin"``.
* **Plan 8 multi-user (D8.26)** — :attr:`Config.dashboard_users` maps
  ``{username: bcrypt_hash}`` (parsed from
  ``RELAY_DASHBOARD_USERS_RAW``). On match the bcrypt hash is
  verified with :func:`bcrypt.checkpw`; malformed hashes raise
  ``ValueError`` which we catch and reject as invalid credentials
  (defends against an operator pasting a non-bcrypt string into the
  env var). The cookie session key is the same as the legacy admin
  path (``"dashboard_user"`` — see
  :data:`gg_relay.api.middleware.dashboard_cookie.SESSION_KEY`) so
  the :class:`DashboardCookieMiddleware` can resolve any logged-in
  user (admin OR a configured dashboard_user) into the synthetic
  ``X-API-Key`` injection contract.

The session middleware (added by the parent app, NOT here) signs
cookies with ``Config.dashboard_session_secret``.

The router rejects any non-``/dashboard/login`` request that lacks a
valid session cookie. All rendered values come from the redacted
session-detail payload — there is no raw spec, frame, or credential
template variable; the only string-data fields are pre-masked by
:class:`RedactionEngine` upstream.

Plan 6 adds a Kanban board (``/dashboard/kanban``) backed by:
  * HTMX 5s polling fallback over the full board (``hx-trigger='every 5s'``)
  * SSE deltas (``/dashboard/kanban/stream``) replacing single cards
    via HTMX SSE extension's ``sse-swap='kanban-update'``
  * A global tokens/cost chart fed by
    :meth:`SessionRepository.aggregate_tokens_by_bucket` (Plan 6 Task 8).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import urllib.parse
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import SecretStr

from gg_relay.api.dependencies.require_role import ROLE_HIERARCHY
from gg_relay.api.deps import get_coordinator, get_manager
from gg_relay.api.middleware.dashboard_cookie import SESSION_KEY as _COOKIE_SESSION_KEY
from gg_relay.core import EventBus, SessionCreated, SessionStateChanged
from gg_relay.session.hitl.coordinator import HITLCoordinator, HITLNotPending
from gg_relay.session.manager import SessionManager, SessionNotFound
from gg_relay.store import (
    CursorFilterMismatchError,
    CursorInvalidError,
    SessionRepository,
)

_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Plan 8 D8.26 — single session key shared with
# :data:`gg_relay.api.middleware.dashboard_cookie.SESSION_KEY`. The
# middleware reads the same key to resolve cookie → username for
# header injection on ``/api/v1/*`` mutations. The Plan 7 legacy
# value (``"user"``) is deliberately retired: a unified key keeps the
# cookie-vs-middleware contract single-sourced and means the audit /
# role-check pipeline doesn't need a compat fallback.
SESSION_USER_KEY = "dashboard_user"


def _require_session(request: Request) -> None:
    """Reject if the session cookie does not carry an authenticated user."""
    user = request.session.get(SESSION_USER_KEY) if hasattr(
        request, "session"
    ) else None
    if not user:
        raise HTTPException(
            status_code=303, headers={"Location": "/dashboard/login"}
        )


# Module-level ``Depends`` instances — using them as default args keeps
# ruff B008 happy (no inline function calls in defaults).
_RequireSessionDep = Depends(_require_session)
_ManagerDep = Depends(get_manager)
_CoordinatorDep = Depends(get_coordinator)


@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "login.html", {"error": None}
    )


def _verify_dashboard_user(
    cfg: Any, username: str, password: str
) -> bool:
    """Plan 8 D8.26 — bcrypt-verify a configured dashboard user.

    Returns ``False`` for unknown users, mismatched passwords, or any
    bcrypt error (``ValueError`` on malformed hash,
    ``UnicodeEncodeError`` on non-utf-8 password — neither should
    crash the login route into a 500).
    """
    users: dict[str, str] = getattr(cfg, "dashboard_users", {}) or {}
    expected_hash = users.get(username)
    if not expected_hash:
        return False
    try:
        import bcrypt

        return bool(
            bcrypt.checkpw(password.encode("utf-8"), expected_hash.encode("utf-8"))
        )
    except (ValueError, UnicodeEncodeError, TypeError):  # pragma: no cover - defensive
        return False
    except ImportError:  # pragma: no cover - bcrypt is a hard dep
        return False


def _verify_admin(
    cfg: Any, username: str, password: str
) -> bool:
    """Legacy single-admin path (D4.11). Kept for backward compat with
    deployments that only set ``RELAY_DASHBOARD_ADMIN_PASSWORD``."""
    admin_pw: SecretStr | None = getattr(cfg, "dashboard_admin_password", None)
    if admin_pw is None or username != "admin":
        return False
    return secrets.compare_digest(password, admin_pw.get_secret_value())


@router.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Any:
    """Handle login form submission.

    Plan 8 D8.26 — check the configured ``dashboard_users`` (bcrypt)
    first; fall back to the legacy admin/``dashboard_admin_password``
    flow so existing deployments keep working. Both paths set the
    same ``SESSION_USER_KEY`` so the cookie middleware can resolve
    either identity into the synthetic ``X-API-Key`` injection
    contract.
    """
    cfg = request.app.state.config
    authenticated = _verify_dashboard_user(
        cfg, username, password
    ) or _verify_admin(cfg, username, password)
    if not authenticated:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "invalid credentials"},
            status_code=401,
        )
    request.session[SESSION_USER_KEY] = username
    return RedirectResponse(url="/dashboard/sessions", status_code=303)


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.pop(SESSION_USER_KEY, None)
    return RedirectResponse(url="/dashboard/login", status_code=303)


@router.get("/sessions", response_class=HTMLResponse)
async def sessions_list(
    request: Request,
    _: None = _RequireSessionDep,
    manager: SessionManager = _ManagerDep,
) -> HTMLResponse:
    rows, _next_cursor = await manager.list(limit=200)
    return templates.TemplateResponse(
        request, "sessions_list.html", {"sessions": rows}
    )


@router.get("/favorites", response_class=HTMLResponse)
async def favorites_page(
    request: Request,
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """Render the logged-in user's "My Favorites" table (Plan 8 D8.21 / Task 13).

    Identity resolution mirrors :func:`session_audit_timeline` —
    :func:`_dashboard_label` collapses the cookie session to the
    ``dashboard-<username>`` label so the same identity used by the
    API ``POST /sessions/{sid}/favorite`` star action drives the
    list query here. A missing label means the cookie middleware
    didn't see a session; we render the empty state rather than
    surface a confusing 401 because the upstream session-required
    dependency would already have redirected un-authed callers.
    """
    store: SessionRepository = request.app.state.store
    label = _dashboard_label(request)
    items: list[dict[str, Any]] = []
    if label:
        rows = await store.list_favorites(user_label=label, limit=100)
        for r in rows:
            s = r["session"]
            starred_at = r["starred_at"]
            items.append(
                {
                    "session_id": r["session_id"],
                    "prompt": (s.get("spec_json") or {}).get(
                        "prompt", ""
                    ),
                    "owner": s.get("owner"),
                    "status": s.get("status"),
                    "starred_at": (
                        starred_at.isoformat()
                        if hasattr(starred_at, "isoformat")
                        else starred_at
                    ),
                }
            )
    return templates.TemplateResponse(
        request,
        "favorites.html",
        {"items": items, "current_actor": label},
    )


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session_detail(
    request: Request,
    session_id: str,
    _: None = _RequireSessionDep,
    manager: SessionManager = _ManagerDep,
    coordinator: HITLCoordinator = _CoordinatorDep,
) -> HTMLResponse:
    try:
        detail = await manager.get(session_id, frames_limit=200)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    pending = coordinator.pending_snapshot(session_id=session_id)
    return templates.TemplateResponse(
        request,
        "session_detail.html",
        {
            "detail": detail,
            "pending_hitl": [
                {"req_id": rid, "tool": v["tool"], "args": v["args"]}
                for rid, v in pending.items()
            ],
        },
    )


def _kanban_columns(
    sessions: list[Any],
) -> dict[str, list[Any]]:
    """Group :class:`SessionSummary` rows into the four Kanban columns.

    Column mapping (D6.13):
        * **queued**     — ``SessionState.QUEUED``
        * **running**    — ``SessionState.RUNNING``
        * **paused**     — ``SessionState.PAUSED``
        * **terminal**   — everything else (completed, failed, cancelled,
          interrupted, …). One column keeps the board compact.

    Order within each column is whatever :meth:`SessionManager.list`
    returned, which is newest-first.
    """
    columns: dict[str, list[Any]] = {
        "queued": [],
        "running": [],
        "paused": [],
        "terminal": [],
    }
    for s in sessions:
        state = s.status.value if hasattr(s.status, "value") else str(s.status)
        if state == "queued":
            columns["queued"].append(s)
        elif state == "running":
            columns["running"].append(s)
        elif state == "paused":
            columns["paused"].append(s)
        else:
            columns["terminal"].append(s)
    return columns


@router.get("/kanban", response_class=HTMLResponse)
async def kanban_page(
    request: Request,
    _: None = _RequireSessionDep,
    manager: SessionManager = _ManagerDep,
) -> HTMLResponse:
    """Render the full Kanban board chrome — the inner board fragment
    is fetched separately by HTMX so subsequent 5s polls and SSE swaps
    only re-render the data, not the surrounding navigation."""
    cfg = request.app.state.config
    page_size = int(getattr(cfg, "kanban_default_page_size", 50))
    sessions, next_cursor = await manager.list(limit=page_size)
    columns = _kanban_columns(list(sessions))
    return templates.TemplateResponse(
        request,
        "kanban.html",
        {
            "columns": columns,
            "page_size": page_size,
            "next_cursor": next_cursor,
            "chart_js_cdn": getattr(cfg, "chart_js_cdn", ""),
            "chart_js_offline": bool(getattr(cfg, "chart_js_offline", False)),
        },
    )


@router.get("/kanban/board", response_class=HTMLResponse)
async def kanban_board_partial(
    request: Request,
    after: str | None = Query(None),
    _: None = _RequireSessionDep,
    manager: SessionManager = _ManagerDep,
) -> HTMLResponse:
    """HTMX target: returns just the inner ``_kanban_board.html``
    fragment — used both by the 5s ``hx-trigger='every 5s'`` polling
    fallback AND by the ``revealed`` cursor lazy-loader.

    Plan 7 D7.6 / Task 9: pagination switched from numeric ``offset``
    to opaque ``after`` cursor so the dashboard cannot drop or duplicate
    rows when a new session lands between two scroll-loads.

    Garbage cursors return a 400 — the page-1 polling fallback recovers
    immediately on the next ``hx-trigger='every 5s'`` tick, so a single
    bad lazy-load is self-healing.
    """
    cfg = request.app.state.config
    page_size = int(getattr(cfg, "kanban_default_page_size", 50))
    try:
        sessions, next_cursor = await manager.list(
            limit=page_size, after=after
        )
    except (CursorFilterMismatchError, CursorInvalidError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    columns = _kanban_columns(list(sessions))
    return templates.TemplateResponse(
        request,
        "_kanban_board.html",
        {
            "columns": columns,
            "page_size": page_size,
            "next_cursor": next_cursor,
        },
    )


# Plan 6 D6.3=A': SSE event classes the kanban stream forwards to the
# browser. Anything else stays on the bus and is ignored — keeps the
# stream lean so the HTMX SSE extension's per-card DOM swap stays cheap.
_KANBAN_STREAM_EVENT_CLASSES = (
    SessionCreated,
    SessionStateChanged,
    "SessionCompleted",  # forward-compat: pulled by str-topic too
)


async def _kanban_sse_iter(
    bus: EventBus,
    request: Request,
    *,
    max_chunks: int | None = None,
) -> AsyncIterator[str]:
    """Stream kanban-update SSE payloads.

    Wire format is the plain ``event: <name>\\ndata: <json>\\n\\n``
    text-event-stream so we don't need the sse-starlette helper here —
    the consumer JS on the kanban page parses ``event`` to decide
    whether to add, replace, or remove a DOM card.

    We pump every subscribed topic into a single :class:`asyncio.Queue`
    via background drain tasks so the main loop can interleave events
    and keep-alives without inspecting each iterator individually.
    Disconnect detection lives in ``finally`` — if the client goes
    away the outer ``StreamingResponse`` cancels the generator and the
    drain tasks shut down cleanly.

    ``max_chunks`` is a test-only escape hatch (set via the
    ``X-Test-Max-Chunks`` request header in the route handler) so the
    integration tests can exercise the HTTP surface end-to-end without
    hanging ASGITransport's buffered response collector. Production
    callers should NEVER set this — the route handler ignores any
    header it didn't write itself.
    """
    del request  # disconnect handled by generator cancellation
    iterators: list[Any] = []
    for ev_spec in _KANBAN_STREAM_EVENT_CLASSES:
        iterators.append(bus.subscribe(ev_spec))

    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=256)

    async def _drain(it: Any) -> None:
        try:
            async for event in it:
                ev_name = (
                    type(event).__name__
                    if hasattr(event, "event_id")
                    else str(getattr(event, "type", "frame"))
                )
                await queue.put((ev_name, event))
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover — bus iterator failures
            return

    drain_tasks = [asyncio.create_task(_drain(it)) for it in iterators]
    emitted = 0
    try:
        yield ": kanban-stream-connected\n\n"
        emitted += 1
        if max_chunks is not None and emitted >= max_chunks:
            return
        while True:
            try:
                ev_name, event = await asyncio.wait_for(
                    queue.get(), timeout=5.0
                )
            except TimeoutError:
                yield ": keep-alive\n\n"
                emitted += 1
                if max_chunks is not None and emitted >= max_chunks:
                    return
                continue
            payload: dict[str, Any] = (
                asdict(event)
                if hasattr(event, "__dataclass_fields__")
                else dict(event)
                if isinstance(event, dict)
                else {"value": str(event)}
            )
            yield (
                f"event: kanban-update\n"
                f"data: {json.dumps({'class': ev_name, 'event': payload}, default=str)}\n\n"
            )
            emitted += 1
            if max_chunks is not None and emitted >= max_chunks:
                return
    finally:
        for t in drain_tasks:
            t.cancel()
        for it in iterators:
            close = getattr(it, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()


@router.get("/kanban/stream")
async def kanban_stream(
    request: Request,
    _: None = _RequireSessionDep,
) -> StreamingResponse:
    """SSE stream that pushes kanban-update events to the browser.

    Subscribes to :class:`SessionCreated`, :class:`SessionStateChanged`
    and (via str-topic) ``SessionCompleted`` so the browser's tiny JS
    snippet can move / re-render the affected card in place without a
    full board reload. The 5s ``hx-trigger='every 5s'`` polling
    fallback on the board partial guards against SSE disconnects.
    """
    bus: EventBus = request.app.state.bus
    # Test-only escape hatch: see ``_kanban_sse_iter`` docstring. The
    # header is namespaced + invisible to production clients so we
    # accept it without conditioning on environment.
    raw_max = request.headers.get("X-Test-Max-Chunks")
    max_chunks: int | None = None
    if raw_max is not None:
        try:
            max_chunks = max(1, int(raw_max))
        except ValueError:
            max_chunks = None
    return StreamingResponse(
        _kanban_sse_iter(bus, request, max_chunks=max_chunks),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx — keep events un-buffered
        },
    )


@router.get("/kanban/chart")
async def kanban_chart(
    request: Request,
    window_s: int = Query(3600, ge=60, le=86400 * 30),
    bucket_s: int = Query(60, ge=15, le=86400),
    _: None = _RequireSessionDep,
) -> dict[str, Any]:
    """Global chart data — Chart.js v4 reads this directly.

    Returns ``{"labels": [...], "datasets": [...]}`` matching the
    Chart.js line-chart schema so the on-page JS can ``new Chart(ctx,
    { type: 'line', data: <this-json> })`` with zero transformation.
    Three datasets are emitted: input tokens, output tokens, cost
    (USD × 1000 for comparable y-axis scale).
    """
    store: SessionRepository = request.app.state.store
    rows = await store.aggregate_tokens_by_bucket(
        window_s=window_s, bucket_s=bucket_s
    )
    labels = [r["bucket_start"].isoformat() for r in rows]
    return {
        "labels": labels,
        "datasets": [
            {
                "label": "input tokens",
                "data": [int(r["input_tokens"]) for r in rows],
            },
            {
                "label": "output tokens",
                "data": [int(r["output_tokens"]) for r in rows],
            },
            {
                "label": "cost (USD × 1000)",
                "data": [float(r["cost_usd"]) * 1000.0 for r in rows],
            },
        ],
        "window_s": window_s,
        "bucket_s": bucket_s,
        "session_count": sum(int(r["sessions"]) for r in rows),
    }


@router.get("/sessions/{session_id}/chart")
async def session_chart(
    request: Request,
    session_id: str,
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """Per-session aggregate chart partial (Plan 6 D6.4).

    Renders the four aggregate values harvested from the session.end
    frame (input_tokens, output_tokens, cost_usd × 1000 for unit
    parity, turn_count) as a stacked horizontal bar via Chart.js. The
    chart itself is read-only — we never wire mutation back from the
    canvas (D6.13).

    Sessions still in-flight emit zeros until session.end lands; the
    template handles ``totals == [0, 0, 0, 0]`` by surfacing a muted
    placeholder so the operator isn't tricked into reading absent
    data.
    """
    store: SessionRepository = request.app.state.store
    row = await store.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    cfg = request.app.state.config
    totals = [
        int(row.get("input_tokens", 0) or 0),
        int(row.get("output_tokens", 0) or 0),
        float(row.get("cost_usd", 0.0) or 0.0) * 1000.0,
        int(row.get("turn_count", 0) or 0),
    ]
    return templates.TemplateResponse(
        request,
        "session_chart.html",
        {
            "session_id": session_id,
            "totals": totals,
            "labels": [
                "input tokens",
                "output tokens",
                "cost (USD × 1000)",
                "turn count",
            ],
            "chart_js_cdn": getattr(cfg, "chart_js_cdn", ""),
        },
    )


@router.get("/sessions/{session_id}/trace")
async def session_trace(
    request: Request,
    session_id: str,
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """Span-tree iframe partial (Plan 6 D6.6=A + D6.14).

    When ``Config.jaeger_ui_url`` is set, embeds the Jaeger UI's
    ``/trace/{trace_id}`` view inside an iframe so operators can drill
    into per-call spans without leaving the dashboard. Production
    deployments **MUST** route ``/jaeger/*`` through the nginx reverse
    proxy in ``deploy/nginx/jaeger-proxy.conf`` so ``X-Frame-Options``
    doesn't deny the embed (D6.14).

    When ``jaeger_ui_url`` is unset OR the session has no ``trace_id``
    (e.g. OTel disabled), falls back to a plain trace-id readout with
    a disabled ``Open in Jaeger`` button — explicit empty-state per
    D6.6's "外链 fallback" wording.
    """
    store: SessionRepository = request.app.state.store
    row = await store.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    cfg = request.app.state.config
    return templates.TemplateResponse(
        request,
        "span_tree.html",
        {
            "session_id": session_id,
            "trace_id": row.get("trace_id"),
            "jaeger_ui_url": getattr(cfg, "jaeger_ui_url", None),
        },
    )


def _dashboard_label(request: Request) -> str | None:
    """Resolve the logged-in dashboard user → ``dashboard-<username>`` label.

    The :class:`DashboardCookieMiddleware` writes the username to
    ``request.state.dashboard_user`` on every request (regardless of
    path) when a valid cookie session is present. We mirror its
    ``dashboard-<username>`` label format here so the ownership /
    role checks line up with the same identity the audit log
    records via the synthetic ``X-API-Key`` injection on
    ``/api/v1/*`` mutations.

    Falls back to reading ``request.session[SESSION_USER_KEY]``
    directly so the function still works if the cookie middleware
    is bypassed in a test fixture (legacy admin path).
    """
    username = getattr(request.state, "dashboard_user", None)
    if not username and hasattr(request, "session"):
        username = request.session.get(_COOKIE_SESSION_KEY)
    if not isinstance(username, str) or not username:
        return None
    return f"dashboard-{username}"


# ── Plan 8 D8.20 / Task 12 — search page + HTMX results fragment ──────


def _dashboard_role(request: Request) -> str:
    """Resolve the dashboard user's role using the same lookup as the
    audit / comments fragments. Falls back to ``viewer`` when no cookie
    or no role mapping is configured — keeping the safe default
    consistent with :func:`gg_relay.api.dependencies.require_role._resolve_role`.
    """
    cfg = request.app.state.config
    label = _dashboard_label(request)
    role_map: dict[str, str] = getattr(cfg, "role_mapping", {}) or {}
    if not label:
        return "viewer"
    return role_map.get(label, "viewer")


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str | None = Query(None, max_length=200),
    owner: str | None = Query(None, max_length=64),
    tags: str | None = Query(None, max_length=512),
    status: str | None = Query(None, max_length=32),
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """Render the search chrome (form + empty results target).

    Plan 8 D8.20 / Task 12. The page itself never runs the query — the
    form submits via HTMX to ``/dashboard/search/results``, which
    renders the result fragment in place. This keeps the initial page
    snappy and lets the form re-target on every change without a full
    reload.

    ``owner`` is rendered into the form ONLY for admin callers; for
    non-admin users the input is omitted (the server-side handler
    rejects cross-owner filters anyway, but trimming the field in the
    UI avoids the false-affordance of "you typed it but it gets
    overridden").
    """
    role = _dashboard_role(request)
    is_admin = ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY["admin"]
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "q": q,
            "owner": owner if is_admin else None,
            "tags_str": tags,
            "status": status,
            "is_admin": is_admin,
        },
    )


@router.get("/search/results", response_class=HTMLResponse)
async def search_results(
    request: Request,
    q: str | None = Query(None, max_length=200),
    owner: str | None = Query(None, max_length=64),
    tags: str | None = Query(None, max_length=512),
    status: str | None = Query(None, max_length=32),
    after: str | None = Query(None, max_length=512),
    limit: int = Query(50, ge=1, le=200),
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """HTMX fragment rendering the search result table + load-more link.

    Plan 8 D8.20 / Task 12. RBAC mirrors the API endpoint: non-admin
    callers are silently force-filtered to ``owner=<self-label>``
    (the UI never lets them type a foreign owner anyway — see
    :func:`search_page`). Cursor errors render as a small inline
    ``<div class='error'>`` rather than a redirect so the HTMX swap
    surfaces the failure inside the panel.
    """
    store: SessionRepository = request.app.state.store
    label = _dashboard_label(request)
    role = _dashboard_role(request)

    if ROLE_HIERARCHY.get(role, 0) < ROLE_HIERARCHY["admin"]:
        owner = label
    elif owner == "":
        owner = None

    tags_list = [t.strip() for t in (tags or "").split(",") if t.strip()] or None
    status_list = [status] if status else None

    try:
        rows, next_cursor = await store.search_sessions(
            q=q or None,
            owner=owner,
            tags=tags_list,
            status=status_list,
            after=after,
            limit=limit,
        )
    except (CursorInvalidError, CursorFilterMismatchError):
        return HTMLResponse(
            "<div class='error'>Invalid cursor.</div>", status_code=400
        )

    qs_dict = {
        k: v
        for k, v in {
            "q": q,
            "owner": owner,
            "tags": tags,
            "status": status,
            "limit": str(limit) if limit != 50 else None,
        }.items()
        if v
    }
    querystring = urllib.parse.urlencode(qs_dict)

    items: list[dict[str, Any]] = []
    for r in rows:
        spec = r.get("spec_json") if hasattr(r, "get") else None
        prompt_val = ""
        if isinstance(spec, dict):
            prompt_val = str(spec.get("prompt") or "")
        submitted = r.get("submitted_at") if hasattr(r, "get") else None
        items.append(
            {
                "id": r["id"],
                "prompt": prompt_val,
                "owner": r.get("owner") if hasattr(r, "get") else None,
                "status": r["status"],
                "tags": list(r["tags"] or []),
                "submitted_at": (
                    submitted.isoformat()
                    if hasattr(submitted, "isoformat")
                    else str(submitted)
                ),
            }
        )
    return templates.TemplateResponse(
        request,
        "_search_results.html",
        {
            "items": items,
            "next_cursor": next_cursor,
            "querystring": querystring,
        },
    )


@router.get("/sessions/{session_id}/audit", response_class=HTMLResponse)
async def session_audit_timeline(
    request: Request,
    session_id: str,
    after: str | None = Query(None, max_length=512),
    limit: int = Query(50, ge=1, le=200),
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """HTMX lazy-load audit timeline for the session-detail page (D8.4 / Task 6).

    Permission mirrors the ``/api/v1/audit?session_id=`` rules:

    * ``admin`` (role_mapping[label] == "admin") — sees any session's
      audit timeline.
    * Everyone else — must own the session (label match against
      ``sessions.owner``). The label is derived from the cookie via
      :func:`_dashboard_label` (``dashboard-<username>``), so an
      admin-namespaced dashboard user gets the admin policy through
      ``cfg.role_mapping["dashboard-<username>"] = "admin"``.

    Errors render as small inline ``<div class='error'>...</div>``
    fragments rather than HTTP redirects so the HTMX swap target
    surfaces the failure inside the panel instead of replacing the
    whole page chrome.
    """
    store: SessionRepository = request.app.state.store
    cfg = request.app.state.config

    label = _dashboard_label(request)
    role_map: dict[str, str] = getattr(cfg, "role_mapping", {}) or {}
    role = role_map.get(label, "viewer") if label else "viewer"

    sess = await store.get_session(session_id)
    if sess is None:
        return HTMLResponse(
            "<div class='error'>Session not found.</div>", status_code=404
        )
    if ROLE_HIERARCHY.get(role, 0) < ROLE_HIERARCHY["admin"]:
        owner = sess.get("owner") if hasattr(sess, "get") else None
        if owner != label:
            return HTMLResponse(
                "<div class='error'>Forbidden.</div>", status_code=403
            )

    try:
        rows, next_cursor = await store.list_audit(
            session_id=session_id, after=after, limit=limit
        )
    except (CursorInvalidError, CursorFilterMismatchError):
        return HTMLResponse(
            "<div class='error'>Invalid cursor.</div>", status_code=400
        )

    items = [
        {
            "id": int(r["id"]),
            "ts": (
                r["ts"].isoformat() if hasattr(r["ts"], "isoformat") else r["ts"]
            ),
            "actor": r["actor"],
            "action": r["action"],
            "target_type": r["target_type"],
            "target_id": r["target_id"],
            "metadata": r["metadata_json"] or {},
        }
        for r in rows
    ]
    return templates.TemplateResponse(
        request,
        "_session_audit_timeline.html",
        {
            "session_id": session_id,
            "items": items,
            "next_cursor": next_cursor,
        },
    )


@router.get("/sessions/{session_id}/comments", response_class=HTMLResponse)
async def session_comments_fragment(
    request: Request,
    session_id: str,
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """HTMX endpoint serving the comments fragment (Plan 8 Task 8 / D8.5).

    Read-only render. Mutations (POST / PATCH / DELETE) skip this
    handler and go directly to ``/api/v1/sessions/{sid}/comments`` and
    ``/api/v1/comments/{cid}`` — :class:`DashboardCookieMiddleware`
    (Task 3) injects the synthetic ``X-API-Key`` header so the API
    layer sees the same ``dashboard-<username>`` label this fragment
    rendered with. Author / role enforcement therefore lives entirely
    in :mod:`gg_relay.api.routers.comments`; the visibility toggles in
    the rendered HTML are purely UX hints.

    ``current_actor`` and ``current_role`` are passed to the template
    so it can show / hide the Edit + Delete buttons. They use the same
    ``dashboard-<username>`` label format and ``cfg.role_mapping``
    lookup as :func:`session_audit_timeline` — keeping the two
    fragments single-sourced on identity resolution.
    """
    store: SessionRepository = request.app.state.store
    cfg = request.app.state.config

    label = _dashboard_label(request)
    role_map: dict[str, str] = getattr(cfg, "role_mapping", {}) or {}
    role = role_map.get(label, "viewer") if label else "viewer"

    sess = await store.get_session(session_id)
    if sess is None:
        return HTMLResponse(
            "<div class='error'>Session not found.</div>", status_code=404
        )

    rows = await store.list_comments(session_id=session_id, limit=200)
    items = [
        {
            "id": int(r["id"]),
            "session_id": r["session_id"],
            "author": r["author"],
            "body_markdown": r["body_markdown"],
            "body_html": r["body_html"],
            "created_at": (
                r["created_at"].isoformat()
                if hasattr(r["created_at"], "isoformat")
                else r["created_at"]
            ),
        }
        for r in rows
    ]
    return templates.TemplateResponse(
        request,
        "_session_comments.html",
        {
            "session_id": session_id,
            "items": items,
            "current_actor": label,
            "current_role": role,
        },
    )


@router.get("/comments/{comment_id}/edit", response_class=HTMLResponse)
async def comment_edit_form(
    request: Request,
    comment_id: int,
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """HTMX endpoint serving the inline edit form (author only).

    Resolves the caller's ``dashboard-<username>`` label via
    :func:`_dashboard_label` and refuses to render the form unless
    it matches the comment's stored ``author`` field. The PATCH
    endpoint on the API side re-checks the same condition, so this
    layer is just a UX gate — a tampered DOM cannot bypass the
    server-side rule.

    Returns a 404 ``<li>`` fragment when the comment does not exist
    or has been soft-deleted (so a stale Edit button after a
    concurrent delete swaps cleanly into the row's slot instead of
    leaking a JSON error blob).
    """
    store: SessionRepository = request.app.state.store

    label = _dashboard_label(request)
    c = await store.get_comment(comment_id=comment_id)
    if c is None or c["deleted_at"] is not None:
        return HTMLResponse(
            "<li class='error'>Comment not found.</li>", status_code=404
        )
    if c["author"] != label:
        return HTMLResponse(
            "<li class='error'>Forbidden.</li>", status_code=403
        )

    return templates.TemplateResponse(
        request,
        "_comment_edit_form.html",
        {
            "comment": {
                "id": int(c["id"]),
                "session_id": c["session_id"],
                "body_markdown": c["body_markdown"],
            },
        },
    )


# ── Plan 8 D8.14 / Task 16 — web submit form ──────────────────────────


@router.get("/new", response_class=HTMLResponse)
async def new_session_form(
    request: Request,
    prompt: str | None = Query(None, max_length=50000),
    tags: str | None = Query(None, max_length=512),
    description: str | None = Query(None, max_length=512),
    template: int | None = Query(None, ge=1),
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """Render the web submission form (Plan 8 D8.14 / Task 16).

    Query-string prefill mirrors the legacy GitHub-style "issue
    template" link convention so a comment, README, or runbook can
    deep-link straight into a pre-populated form:

      * ``?prompt=<text>``        — seeds the prompt textarea.
      * ``?tags=foo,bar``         — seeds the tags input as csv.
      * ``?description=<text>``   — seeds the description input.
      * ``?template=<id>``        — pre-loads a saved prompt template
        (overrides the other three above when an explicit value
        wasn't passed). Visibility is enforced server-side: the
        template must be owned by the caller, marked ``shared``, or
        the caller must be an admin (mirrors
        ``GET /api/v1/templates/{id}``). A 404 / 403 result surfaces
        as an inline ``alert-warning`` banner so the form is still
        usable with empty values.

    The form posts to ``POST /api/v1/sessions`` directly (relying on
    the :class:`DashboardCookieMiddleware` synthetic ``X-API-Key``
    injection from Task 3 / D8.26 so the API auth + RBAC + audit
    pipeline runs unchanged). RBAC of the actual submit lives there
    — this handler does not pre-check ``submitter`` because the
    form GET is informational; a viewer-role user can render the
    page but their POST will be rejected at the API boundary.
    """
    store: SessionRepository = request.app.state.store
    cfg = request.app.state.config
    label = _dashboard_label(request)
    role_map: dict[str, str] = getattr(cfg, "role_mapping", {}) or {}
    role = role_map.get(label, "viewer") if label else "viewer"
    is_admin = ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY["admin"]

    template_obj: dict[str, Any] | None = None
    template_load_error: str | None = None
    if template is not None:
        t = await store.get_template(template_id=template)
        if t is None:
            template_load_error = f"Template {template} not found"
        elif (
            not bool(t["shared"])
            and t["creator"] != label
            and not is_admin
        ):
            template_load_error = f"Template {template} is private"
        else:
            template_obj = {
                "id": int(t["id"]),
                "name": t["name"],
                "prompt": t["prompt"],
                "description": t["description"],
                "tags": t["tags"],
                "creator": t["creator"],
            }
            if not prompt:
                prompt = t["prompt"]
            if not description:
                description = t["description"]
            if not tags:
                tags = t["tags"]

    if label is None:
        template_choices: list[dict[str, Any]] = []
    else:
        visible = await store.list_templates(
            actor=label, is_admin=is_admin, limit=200
        )
        template_choices = [
            {
                "id": int(t["id"]),
                "name": t["name"],
                "shared": bool(t["shared"]),
                "creator": t["creator"],
            }
            for t in visible
        ]

    return templates.TemplateResponse(
        request,
        "new.html",
        {
            "prompt": prompt or "",
            "tags": tags or "",
            "description": description or "",
            "template_obj": template_obj,
            "template_load_error": template_load_error,
            "template_choices": template_choices,
            "current_actor": label,
        },
    )


@router.get("/new/check-duplicate", response_class=HTMLResponse)
async def check_duplicate_prompt(
    request: Request,
    prompt: str = Query("", max_length=50000),
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """HTMX fragment: warn when the same owner submitted the same
    prompt prefix in the last 10 minutes.

    Plan 8 D8.14 / Task 16. Returns an empty body when there is
    nothing to warn about, so HTMX swaps an empty fragment into the
    target ``<div id='duplicate-warning'>`` (effectively clearing
    any earlier warning when the user edits the prompt away from
    the duplicate).

    The 5-character minimum guards against the keyup debounce
    issuing a query for "h" / "he" / "hel" while the user is
    starting to type — substring matches that short would generate
    noise without giving the user useful information.
    """
    store: SessionRepository = request.app.state.store
    label = _dashboard_label(request)
    if not label:
        return HTMLResponse("")
    text = (prompt or "").strip()
    if len(text) < 5:
        return HTMLResponse("")
    recent = await store.recent_same_prompt(
        owner=label, prompt=text, within_minutes=10
    )
    if not recent:
        return HTMLResponse("")
    items = [
        {
            "id": r["id"],
            "status": r["status"],
            "submitted_at": (
                r["submitted_at"].isoformat()
                if hasattr(r["submitted_at"], "isoformat")
                else str(r["submitted_at"])
            ),
        }
        for r in recent[:3]
    ]
    return templates.TemplateResponse(
        request,
        "_duplicate_warning.html",
        {"items": items},
    )


@router.get("/templates", response_class=HTMLResponse)
async def templates_page(
    request: Request,
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """Render the logged-in user's "Prompt Templates" page (Plan 8 D8.24 / Task 14).

    Identity resolution mirrors :func:`favorites_page` —
    :func:`_dashboard_label` collapses the cookie session to the
    ``dashboard-<username>`` label so the same identity used by the
    API ``POST /api/v1/templates`` write drives the list query
    here. A missing label means the cookie middleware didn't see a
    session; we render the empty state rather than 401 because the
    upstream session-required dependency would already have
    redirected un-authed callers.

    The ``is_mine`` flag per row decides whether the action column
    renders the Delete button — purely a UX gate; the API endpoint
    re-enforces the rule server-side so a tampered DOM cannot
    bypass it.

    Task 16 (web submit form) will consume ``?template=<id>`` from
    the Use link; until Task 16 lands the link 404s on click, which
    is acceptable per the plan ordering.
    """
    store: SessionRepository = request.app.state.store
    cfg = request.app.state.config
    label = _dashboard_label(request)
    if not label:
        return HTMLResponse(
            "Login required", status_code=401
        )
    role_map: dict[str, str] = getattr(cfg, "role_mapping", {}) or {}
    role = role_map.get(label, "viewer")
    is_admin = ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY["admin"]
    rows = await store.list_templates(
        actor=label, is_admin=is_admin, limit=200
    )
    items = [
        {
            "id": int(r["id"]),
            "name": r["name"],
            "creator": r["creator"],
            "description": r["description"],
            "shared": bool(r["shared"]),
            "tags": r["tags"],
            "is_mine": r["creator"] == label,
        }
        for r in rows
    ]
    return templates.TemplateResponse(
        request,
        "templates.html",
        {
            "items": items,
            "current_actor": label,
            "is_admin": is_admin,
        },
    )


@router.post("/sessions/{session_id}/hitl/{req_id}")
async def session_hitl_resolve(
    request: Request,
    session_id: str,
    req_id: str,
    decision: str = Form(...),
    reason: str | None = Form(default=None),
    _: None = _RequireSessionDep,
    coordinator: HITLCoordinator = _CoordinatorDep,
) -> HTMLResponse:
    if decision not in {"accept", "deny"}:
        raise HTTPException(status_code=400, detail="invalid decision")
    full_req_id = req_id if ":" in req_id else f"{session_id}:{req_id}"
    decision_lit = cast(Literal["accept", "deny"], decision)
    try:
        await coordinator.resolve(full_req_id, decision_lit, reason=reason)
    except HITLNotPending as exc:
        raise HTTPException(
            status_code=409, detail="hitl already resolved"
        ) from exc
    return HTMLResponse(
        f"<div class='hitl-resolved'>{decision} (req_id={full_req_id})</div>"
    )
