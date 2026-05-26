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
import hashlib
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
from gg_relay.api.routers.user_credentials import (
    ALLOWED_ENV_NAMES as _CRED_ALLOWED_ENV_NAMES,
    length_class as _cred_length_class,
    mask_credential_value,
)
from gg_relay.core import EventBus, SessionCreated, SessionState, SessionStateChanged
from gg_relay.core.domain import SessionSummary
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


def _owner_color(owner: str | None) -> str:
    """Plan 8 D8.0 / Task 15 — deterministic HSL color from owner label.

    The hue is derived from the first 8 hex digits of MD5(owner)
    (mod 360); saturation + lightness are pinned so every label gets
    a readable contrast against the dark dashboard panel without an
    ad-hoc palette per owner. ``None``/empty owners fall back to the
    neutral muted gray so the badge can still render without a noisy
    "transparent" slot.

    MD5 is used as a cheap non-cryptographic hash purely for visual
    bucketing; collisions across labels are acceptable and never
    feed into auth / RBAC decisions.
    """
    if not owner:
        return "hsl(0, 0%, 45%)"
    digest = hashlib.md5(owner.encode("utf-8"), usedforsecurity=False).hexdigest()
    hue = int(digest[:8], 16) % 360
    return f"hsl({hue}, 55%, 45%)"


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["owner_color"] = _owner_color


def _ctx_dashboard_role(request: Request) -> str:
    """Jinja global — resolve the current request's dashboard role.

    Wraps :func:`_dashboard_role` (defined further down) but is exposed
    early via a forward-friendly indirection so the global registration
    below can stay co-located with the template loader setup. The
    actual lookup runs on every render so a logged-out request (no
    cookie middleware state) reliably collapses to ``"viewer"``,
    which the sidebar partial uses to gray out create CTAs.
    """
    return _dashboard_role(request)


def _ctx_dashboard_username(request: Request) -> str | None:
    """Jinja global — resolve the cookie-session username.

    Returns ``None`` for anonymous requests so the sidebar/topbar
    user-chip partials can fall back to an unauthenticated state
    without crashing on a missing attribute.
    """
    username = getattr(request.state, "dashboard_user", None)
    if isinstance(username, str) and username:
        return username
    # Fallback to direct cookie read for tests that bypass the
    # cookie middleware (e.g. only the legacy admin path is active).
    if hasattr(request, "session"):
        raw = request.session.get(_COOKIE_SESSION_KEY)
        if isinstance(raw, str) and raw:
            return raw
    return None


templates.env.globals["dashboard_role"] = _ctx_dashboard_role
templates.env.globals["dashboard_username"] = _ctx_dashboard_username

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
    # HTMX 轮询请求只需要表格片段，避免用完整页面替换 <table> 导致内容重复
    template = (
        "_sessions_table.html"
        if request.headers.get("HX-Request")
        else "sessions_list.html"
    )
    return templates.TemplateResponse(
        request, template, {"sessions": rows}
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
    owner: str | None = Query(None, max_length=64),
    status: str | None = Query(None, max_length=32),
    tag: str | None = Query(None, max_length=64),
    _: None = _RequireSessionDep,
    manager: SessionManager = _ManagerDep,
) -> HTMLResponse:
    """Render the full Kanban board chrome — the inner board fragment
    is fetched separately by HTMX so subsequent 5s polls and SSE swaps
    only re-render the data, not the surrounding navigation.

    Plan 8 D8.0 / Task 15 — accepts optional ``owner`` / ``status`` /
    ``tag`` query params. When ANY filter is set the page routes
    through :meth:`SessionRepository.search_sessions` (filter-aware
    cursor pagination, same path the search page uses) and the 5s
    polling fallback's next-page lazy-loader is silenced to avoid
    clobbering the filtered view with un-filtered polls. With no
    filter set the behavior is unchanged from Plan 6 (manager.list
    → SessionSummary, 5s polling enabled).

    RBAC mirrors :func:`search_results`: non-admin callers are
    silently force-filtered to their own ``dashboard-<self>`` label
    so a submitter cannot peek at another team's queue by typing a
    foreign owner into the URL. Admin callers see what they asked
    for (or the un-filtered firehose).
    """
    cfg = request.app.state.config
    page_size = int(getattr(cfg, "kanban_default_page_size", 50))

    has_filter = bool(owner or status or tag)
    if has_filter:
        sessions, _next_cursor, effective_owner = await _filtered_summaries(
            request, owner=owner, status=status, tag=tag, limit=page_size
        )
        # The 5s polling URL is the bare /dashboard/kanban/board (no
        # filter context) so we suppress the next-page lazy-loader
        # here — filtered users can still navigate via the filter
        # form. The list view carries proper filter-aware pagination.
        next_cursor: str | None = None
    else:
        sessions_summaries, next_cursor = await manager.list(limit=page_size)
        sessions = list(sessions_summaries)
        effective_owner = owner

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
            "filter_owner": effective_owner,
            "filter_status": status,
            "filter_tag": tag,
            "has_filter": has_filter,
        },
    )


def _summary_from_search_row(row: Any) -> SessionSummary:
    """Convert a ``search_sessions`` raw row mapping into a
    :class:`SessionSummary` so the kanban template can render filtered
    results without a parallel rendering path.
    """
    return SessionSummary(
        id=row["id"],
        status=SessionState(row["status"]),
        submitted_at=row["submitted_at"],
        started_at=row.get("started_at") if hasattr(row, "get") else row["started_at"],
        ended_at=row.get("ended_at") if hasattr(row, "get") else row["ended_at"],
        tags=tuple(row["tags"] or ()),
        backend=(row.get("backend") if hasattr(row, "get") else row["backend"]) or "",
        end_reason=row.get("end_reason") if hasattr(row, "get") else row["end_reason"],
        owner=row.get("owner") if hasattr(row, "get") else None,
    )


async def _filtered_summaries(
    request: Request,
    *,
    owner: str | None,
    status: str | None,
    tag: str | None,
    limit: int,
    after: str | None = None,
) -> tuple[list[SessionSummary], str | None, str | None]:
    """Resolve filters → ``(summaries, next_cursor, effective_owner)``.

    Shared by the kanban filter wiring and the list view so RBAC + the
    ``search_sessions`` filter shape stay single-sourced. ``owner`` is
    forced to the caller's ``dashboard-<self>`` label for non-admin
    callers (single identity contract, D8.25); admin callers see
    whatever they typed (or all owners when blank).

    Cursor errors collapse to an empty page so the rendered view
    doesn't 500 on a stale URL share.
    """
    store: SessionRepository = request.app.state.store
    label = _dashboard_label(request)
    role = _dashboard_role(request)
    is_admin = ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY["admin"]

    if not is_admin and label:
        effective_owner: str | None = label
    elif owner == "":
        effective_owner = None
    else:
        effective_owner = owner

    status_list = [status] if status else None
    tags_list = [tag] if tag else None

    try:
        rows, next_cursor = await store.search_sessions(
            owner=effective_owner,
            tags=tags_list,
            status=status_list,
            after=after,
            limit=limit,
        )
    except (CursorInvalidError, CursorFilterMismatchError):
        return [], None, effective_owner

    summaries = [_summary_from_search_row(r) for r in rows]
    return summaries, next_cursor, effective_owner


@router.get("/list", response_class=HTMLResponse)
async def sessions_list_view(
    request: Request,
    owner: str | None = Query(None, max_length=64),
    status: str | None = Query(None, max_length=32),
    tag: str | None = Query(None, max_length=64),
    after: str | None = Query(None, max_length=512),
    limit: int = Query(50, ge=1, le=200),
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """Plan 8 Task 15 / D8.0 — list-view table with cursor pagination.

    HTMX-aware: when the client sends ``HX-Request: true`` AND an
    ``after`` cursor (i.e. the load-more row revealed itself), only
    the rows-fragment is returned so the existing tbody appends in
    place. Otherwise the full page chrome renders so navigation /
    deep links work without JS.

    RBAC and filter shaping are shared with the kanban filter wiring
    via :func:`_filtered_summaries` — non-admin callers always see
    their own owner regardless of what they typed in the form.

    The fragment branch must precede the full page branch because
    HTMX users still load the full page on first navigation (no
    ``after``) — the cursor presence is the disambiguator.
    """
    summaries, next_cursor, effective_owner = await _filtered_summaries(
        request,
        owner=owner,
        status=status,
        tag=tag,
        limit=limit,
        after=after,
    )

    items = [_list_item(s) for s in summaries]

    qs_dict = {
        k: v
        for k, v in {
            "owner": owner,
            "status": status,
            "tag": tag,
            "limit": str(limit) if limit != 50 else None,
        }.items()
        if v
    }
    querystring = urllib.parse.urlencode(qs_dict)

    ctx = {
        "items": items,
        "next_cursor": next_cursor,
        "querystring": querystring,
        "filter_owner": effective_owner,
        "filter_status": status,
        "filter_tag": tag,
    }

    hx_request = request.headers.get("HX-Request") == "true"
    if hx_request and after:
        return templates.TemplateResponse(
            request, "_list_rows.html", ctx
        )

    return templates.TemplateResponse(request, "list.html", ctx)


def _list_item(s: SessionSummary) -> dict[str, Any]:
    """Project a :class:`SessionSummary` into the list-row dict.

    Keeps the template free of attribute access on the dataclass so
    the same partial can be reused later if we switch the data source
    (e.g. add favorites / parent_session columns in Plan 9).
    """
    return {
        "id": s.id,
        "owner": s.owner,
        "status": s.status.value if hasattr(s.status, "value") else str(s.status),
        "submitted_at": s.submitted_at.isoformat()
        if hasattr(s.submitted_at, "isoformat")
        else str(s.submitted_at),
        "tags": list(s.tags),
        "backend": s.backend,
        "end_reason": s.end_reason,
    }


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


_LEGACY_ADMIN_LABEL = "dashboard-admin"


def _dashboard_role(request: Request) -> str:
    """Resolve the dashboard user's role using the same lookup as the
    audit / comments fragments.

    Resolution order:

    1. Anonymous (no cookie) → ``viewer``.
    2. Explicit ``role_mapping`` hit → that role.
    3. ``dashboard-admin`` (legacy admin path via
       ``dashboard_admin_password``) → ``admin``. This is the only
       way to log in as ``admin`` in installations that haven't
       configured ``role_mapping_raw``; without this fallback the
       legacy admin lands as ``viewer`` and every "+ New session"
       affordance renders as a disabled ``<span>`` (the reported
       "clicking New Session does nothing" bug).
    4. Otherwise → ``viewer`` (matches
       :func:`gg_relay.api.dependencies.require_role._resolve_role`).
    """
    cfg = request.app.state.config
    label = _dashboard_label(request)
    if not label:
        return "viewer"
    role_map: dict[str, str] = getattr(cfg, "role_mapping", {}) or {}
    if label in role_map:
        return role_map[label]
    if label == _LEGACY_ADMIN_LABEL and getattr(
        cfg, "dashboard_admin_password", None
    ):
        return "admin"
    return "viewer"


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
        if submitted is not None and hasattr(submitted, "isoformat"):
            submitted_str = str(submitted.isoformat())
        else:
            submitted_str = str(submitted)
        items.append(
            {
                "id": r["id"],
                "prompt": prompt_val,
                "owner": r.get("owner") if hasattr(r, "get") else None,
                "status": r["status"],
                "tags": list(r["tags"] or []),
                "submitted_at": submitted_str,
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


def _cmdk_pages(can_submit: bool, is_admin: bool) -> list[dict[str, str]]:
    """Static navigation entries surfaced by the command palette.

    Kept in router (not template) so the same list can be filtered
    server-side against the typed query and pinned by tests without
    parsing template loops. Mirrors the sidebar order.
    """
    pages: list[dict[str, str]] = [
        {
            "href": "/dashboard/overview", "icon": "▦",
            "label": "Overview", "hint": "Operator dashboard",
        },
        {
            "href": "/dashboard/kanban", "icon": "▤",
            "label": "Kanban", "hint": "Lifecycle board",
        },
        {
            "href": "/dashboard/list", "icon": "☰",
            "label": "List", "hint": "Table view",
        },
        {
            "href": "/dashboard/sessions", "icon": "◴",
            "label": "Live feed", "hint": "Auto-refresh",
        },
        {
            "href": "/dashboard/search", "icon": "⌕",
            "label": "Search", "hint": "Filter sessions",
        },
        {
            "href": "/dashboard/favorites", "icon": "★",
            "label": "Favorites", "hint": "Starred sessions",
        },
        {
            "href": "/dashboard/templates", "icon": "▣",
            "label": "Templates", "hint": "Saved prompts",
        },
        {
            "href": "/dashboard/cost", "icon": "¤",
            "label": "Cost", "hint": "Per-owner spend",
        },
    ]
    if can_submit:
        pages.append(
            {
                "href": "/dashboard/me/credentials", "icon": "⚷",
                "label": "My credentials",
                "hint": "Per-user upstream keys",
            }
        )
    if is_admin:
        pages.extend(
            [
                {
                    "href": "/dashboard/admin/keys", "icon": "⚿",
                    "label": "API keys", "hint": "Admin only",
                },
                {
                    "href": "/dashboard/admin/credentials", "icon": "⚷",
                    "label": "Credentials",
                    "hint": "Per-user upstream — admin only",
                },
            ]
        )
    return pages


def _cmdk_quick_actions(can_submit: bool) -> list[dict[str, str]]:
    """Action shortcuts surfaced at the top of the palette.

    Only emitted for callers who can actually perform them (RBAC-gated
    on the server side instead of disabling client-side — the palette
    must never surface a disabled affordance because there's no place
    to render the explanation).
    """
    if not can_submit:
        return []
    return [
        {
            "href": "/dashboard/new",
            "icon": "+",
            "label": "Submit new session",
            "hint": "Create from prompt or template",
        },
        {
            "href": "/dashboard/templates",
            "icon": "▣",
            "label": "Open templates",
            "hint": "Save or reuse a prompt",
        },
    ]


async def _cmdk_recent_sessions(
    request: Request,
    q: str | None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Recent sessions surfaced in the palette.

    For non-admin callers we always filter by ``owner=<self-label>``
    so the palette can't leak peers' work. Admins see the global
    most-recent list.

    When ``q`` is set, we use ``search_sessions`` so the same prefix
    / substring rules the dedicated search page uses apply.
    """
    store: SessionRepository = request.app.state.store
    label = _dashboard_label(request)
    role = _dashboard_role(request)
    is_admin = ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY["admin"]
    owner = None if is_admin else label
    try:
        rows, _next = await store.search_sessions(
            q=q or None,
            owner=owner,
            tags=None,
            status=None,
            after=None,
            limit=limit,
        )
    except (CursorInvalidError, CursorFilterMismatchError):
        return []
    items: list[dict[str, Any]] = []
    for r in rows:
        spec = r.get("spec_json") if hasattr(r, "get") else None
        prompt_val = ""
        if isinstance(spec, dict):
            prompt_val = str(spec.get("prompt") or "")
        prompt_excerpt = prompt_val[:48].replace("\n", " ").strip() if prompt_val else ""
        items.append(
            {
                "id": r["id"],
                "status": r["status"],
                "owner": r.get("owner") if hasattr(r, "get") else None,
                "prompt_excerpt": prompt_excerpt,
            }
        )
    return items


@router.get("/cmdk", response_class=HTMLResponse)
async def cmdk_modal(
    request: Request,
    q: str | None = Query(None, max_length=200),
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """Command palette modal shell + initial results.

    Returned as an HTMX fragment that the global keybind in base.html
    swaps into ``#cmdk-mount`` when the user presses ⌘K / Ctrl+K.
    """
    role = _dashboard_role(request)
    is_admin = ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY["admin"]
    can_submit = role in ("submitter", "admin")
    return templates.TemplateResponse(
        request,
        "_cmdk_modal.html",
        {
            "q": q or "",
            "quick_actions": _cmdk_quick_actions(can_submit),
            "pages": _cmdk_pages(can_submit, is_admin),
            "recent_sessions": await _cmdk_recent_sessions(request, q),
            "can_submit": can_submit,
            "is_admin": is_admin,
        },
    )


@router.get("/cmdk/results", response_class=HTMLResponse)
async def cmdk_results(
    request: Request,
    q: str | None = Query(None, max_length=200),
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """Cmdk results fragment — same shape as the modal body so the
    input can swap ``#cmdk-results`` in place as the user types.
    """
    role = _dashboard_role(request)
    is_admin = ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY["admin"]
    can_submit = role in ("submitter", "admin")
    return templates.TemplateResponse(
        request,
        "_cmdk_results.html",
        {
            "q": q or "",
            "quick_actions": _cmdk_quick_actions(can_submit),
            "pages": _cmdk_pages(can_submit, is_admin),
            "recent_sessions": await _cmdk_recent_sessions(request, q),
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

    label = _dashboard_label(request)
    # Use _dashboard_role (not raw role_map lookup) so the legacy
    # admin path (dashboard_admin_password without role_mapping_raw)
    # resolves to "admin" — the same fix B1 applied to the rest of
    # the dashboard router. Bypassing it caused the
    # "admin → 403 on /dashboard/admin/keys" regression.
    role = _dashboard_role(request)

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

    label = _dashboard_label(request)
    # See note on session_audit_timeline — _dashboard_role honors the
    # legacy admin login; the raw role_map lookup does not.
    role = _dashboard_role(request)

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
    label = _dashboard_label(request)
    # See note on session_audit_timeline — _dashboard_role honors the
    # legacy admin login; the raw role_map lookup does not.
    role = _dashboard_role(request)
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


@router.get("/", response_class=HTMLResponse)
async def dashboard_root(
    request: Request,
    _: None = _RequireSessionDep,
    manager: SessionManager = _ManagerDep,
) -> Any:
    """Per-role default view (Plan 8 D8.30 / Task 23 step 5).

    Submitters (and viewers) are redirected to their own kanban
    slice — ``/dashboard/kanban?owner=<self-label>`` — so the
    landing experience matches the "show me MY work" mental model
    operators actually have. Admins see the full unfiltered kanban
    (same path as the explicit ``/dashboard/kanban`` link in the
    nav) because their job is the team-wide view.

    HTTP 302 was chosen over a JS-only behaviour for three reasons:

      1. Server-side preserves the redirect across browser tabs
         opened from links (the operator's bookmark stays correct).
      2. The dashboard's HTMX layer never sees this path — kanban
         polling continues to talk to ``/dashboard/kanban/board``
         directly so the redirect doesn't add a hop to the hot
         path.
      3. ``/dashboard`` is the natural URL for the role-aware
         landing; without this redirect a submitter typing the
         bare URL gets a 404, which is hostile.

    The redirect target carries the caller's owner label as a
    query parameter so :func:`kanban_page` renders the filtered
    slice. Admin callers land on the same handler with no filter,
    matching the unfiltered behaviour the existing
    ``/dashboard/kanban`` nav link drives.
    """
    label = _dashboard_label(request)
    role = _dashboard_role(request)
    is_admin = ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY["admin"]

    if label and not is_admin:
        return RedirectResponse(
            url=f"/dashboard/kanban?owner={urllib.parse.quote(label)}",
            status_code=302,
        )
    # Admin (or anon with no label) — delegate to the kanban page
    # handler so the same template + RBAC logic runs without
    # duplicating the rendering branch here.
    return await kanban_page(
        request,
        owner=None,
        status=None,
        tag=None,
        _=None,
        manager=manager,
    )


@router.get("/overview", response_class=HTMLResponse)
async def overview_page(
    request: Request,
    _: None = _RequireSessionDep,
    manager: SessionManager = _ManagerDep,
) -> HTMLResponse:
    """Multica-aligned operator overview — KPI cards + 24h trend + status mix.

    This is an additive route; the legacy ``/dashboard/`` (role-based
    redirect) and ``/dashboard/cost`` (attribution table) stay intact
    so existing bookmarks / tests are unaffected.

    Data sources all reuse existing repository methods:

    * **Live count** — ``SessionManager.list(limit=200)`` then filter
      by ``status`` so a fresh deploy with no rows still renders zeros
      instead of an empty page.
    * **24h tokens / cost** — ``SessionRepository.aggregate_tokens_by_bucket``
      summed across all hourly buckets; the same store call powers the
      kanban global chart, guaranteeing the two views show identical
      numbers without a parallel aggregation path.
    * **Status mix** — derived from the same in-memory ``sessions``
      list via :func:`_kanban_columns` so the dashboard never double-
      reads the DB for a view that is, by design, a snapshot.

    RBAC: non-admin viewers/submitters see only their own slice of
    the recent-sessions table — admins see the team firehose. This
    mirrors :func:`kanban_page` and avoids the affordance of
    surfacing other owners' sessions just because they fit in the
    KPI bucket query.
    """
    store: SessionRepository = request.app.state.store
    label = _dashboard_label(request)
    role = _dashboard_role(request)
    is_admin = ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY["admin"]

    sessions_summaries, _next_cursor = await manager.list(limit=200)
    sessions = list(sessions_summaries)
    columns = _kanban_columns(sessions)

    live_count = (
        len(columns["queued"]) + len(columns["running"]) + len(columns["paused"])
    )
    total_count = len(sessions)
    status_mix = {
        "queued": len(columns["queued"]),
        "running": len(columns["running"]),
        "paused": len(columns["paused"]),
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
        "interrupted": 0,
    }
    for s in columns["terminal"]:
        state = s.status.value if hasattr(s.status, "value") else str(s.status)
        if state in status_mix:
            status_mix[state] += 1

    bucket_rows = await store.aggregate_tokens_by_bucket(
        window_s=86400, bucket_s=3600
    )
    input_tokens = sum(int(r["input_tokens"]) for r in bucket_rows)
    output_tokens = sum(int(r["output_tokens"]) for r in bucket_rows)
    total_tokens = input_tokens + output_tokens
    total_cost = sum(float(r["cost_usd"]) for r in bucket_rows)
    bucket_sessions = sum(int(r["sessions"]) for r in bucket_rows)

    # Filter by ownership FIRST, then slice — otherwise non-admin
    # users could see an empty recent list even with their own
    # sessions queued behind a noisy admin/other-owner header
    # (Santa Reviewer E findings round 2). For non-admin callers the
    # match is **strict** `owner == label` — un-owned (owner=None)
    # rows are intentionally excluded to avoid surfacing sessions
    # that may have been minted by a system process or another
    # caller (Santa Reviewer G findings round 3).
    if is_admin:
        recent_pool = sessions
    elif label:
        recent_pool = [
            s for s in sessions if getattr(s, "owner", None) == label
        ]
    else:
        recent_pool = []

    recent_rows: list[dict[str, Any]] = []
    for s in recent_pool[:5]:
        recent_rows.append(
            {
                "id": s.id,
                "status": s.status.value
                if hasattr(s.status, "value")
                else str(s.status),
                "owner": getattr(s, "owner", None),
                "submitted_at": s.submitted_at.isoformat()
                if hasattr(s.submitted_at, "isoformat")
                else str(s.submitted_at),
                "tags": list(getattr(s, "tags", []) or []),
            }
        )

    cfg = request.app.state.config
    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "active_nav": "overview",
            "kpis": {
                "live": live_count,
                "total": total_count,
                "tokens": total_tokens,
                "cost_usd": total_cost,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "bucket_sessions": bucket_sessions,
            },
            "status_mix": status_mix,
            "recent_rows": recent_rows,
            "chart_js_cdn": getattr(cfg, "chart_js_cdn", ""),
            "chart_js_offline": bool(getattr(cfg, "chart_js_offline", False)),
            "role": role,
            "is_admin": is_admin,
            "can_submit": role in ("submitter", "admin"),
        },
    )


@router.get("/cost", response_class=HTMLResponse)
async def cost_page(
    request: Request,
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """Render the per-role cost attribution page (Plan 8 D8.30 / Task 23).

    Admin sees the top-N owners across the team; everyone else sees
    only their own row. The page consumes :meth:`store.summary_for_user`
    for the at-a-glance banner and :meth:`store.aggregate_cost_by_owner`
    for the table — the same two store calls the API endpoints use,
    so the dashboard and API render identical numbers without a
    parallel aggregation path.

    Identity resolution mirrors :func:`favorites_page` /
    :func:`templates_page` — the cookie session collapses to a
    ``dashboard-<username>`` label and the role lookup hits
    ``cfg.role_mapping``. A missing label means the cookie
    middleware didn't see a session; we render an unauthenticated
    placeholder rather than 401 because the upstream session-
    required dependency would already have redirected un-authed
    callers.
    """
    store: SessionRepository = request.app.state.store
    label = _dashboard_label(request)
    role = _dashboard_role(request)
    is_admin = ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY["admin"]

    if not label:
        return HTMLResponse("Login required", status_code=401)

    if is_admin:
        top_owners = await store.aggregate_cost_by_owner(limit=10)
    else:
        # Single-owner slice: aggregate then filter to the caller.
        # We still run the GROUP BY (vs a per-user count + sum) so
        # the same SQL path serves both branches — kept in lockstep
        # with the API endpoint so a future query optimisation
        # propagates uniformly.
        all_rows = await store.aggregate_cost_by_owner(limit=200)
        top_owners = [r for r in all_rows if r.get("owner") == label]

    own_summary = await store.summary_for_user(
        user_label=label, period="this_month"
    )

    return templates.TemplateResponse(
        request,
        "cost.html",
        {
            "top_owners": top_owners,
            "summary": own_summary,
            "current_actor": label,
            "is_admin": is_admin,
        },
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
    label = _dashboard_label(request)
    if not label:
        return HTMLResponse(
            "Login required", status_code=401
        )
    # See note on session_audit_timeline — _dashboard_role honors the
    # legacy admin login; the raw role_map lookup does not (the
    # legacy admin would have been demoted to viewer and lost the
    # admin-only template visibility scope).
    role = _dashboard_role(request)
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


@router.get("/admin/keys", response_class=HTMLResponse)
async def admin_keys_page(
    request: Request,
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """Admin api_key self-service page (Plan 8 D8.29 / Task 22).

    Server-side rendered list + inline create + revoke forms. The
    actual mutations call ``/api/v1/admin/keys`` (POST / DELETE) via
    HTMX so the API-side guards (self-revoke, last-admin,
    409 on duplicate label) drive the user-visible errors. The
    page itself is a read-only render of the current rows so a
    non-admin cookie session that wanders in surfaces a clear
    "Forbidden" instead of seeing the mutation widgets.

    Identity / role resolution mirrors :func:`templates_page` so
    the same admin label that drives ``cfg.role_mapping`` is the
    one this page gates on.
    """
    label = _dashboard_label(request)
    if not label:
        return HTMLResponse("Login required", status_code=401)
    # Bug fix (user-reported): "admin clicks API keys → 403".
    # Previously this used a raw role_map.get(label, "viewer") which
    # bypassed the legacy-admin → admin fallback in _dashboard_role.
    # Operators on the default install (dashboard_admin_password set,
    # role_mapping_raw unset) logged in as admin but landed as
    # viewer here, so the page slammed them with 403 even though
    # the sidebar already showed them the "API keys" link.
    role = _dashboard_role(request)
    is_admin = ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY["admin"]
    if not is_admin:
        return HTMLResponse(
            "<div class='error'>Forbidden — admin only.</div>",
            status_code=403,
        )
    key_store = getattr(request.app.state, "api_key_store", None)
    rows: list[dict[str, Any]] = []
    if key_store is not None:
        raw_rows = await key_store.list(include_revoked=True, limit=200)
        for r in raw_rows:
            rows.append(
                {
                    "label": r["label"],
                    "role": r["role"],
                    "created_at": (
                        r["created_at"].isoformat()
                        if hasattr(r["created_at"], "isoformat")
                        else r["created_at"]
                    ),
                    "created_by_label": r["created_by_label"],
                    "expires_at": (
                        r["expires_at"].isoformat()
                        if r["expires_at"] is not None
                        and hasattr(r["expires_at"], "isoformat")
                        else r["expires_at"]
                    ),
                    "revoked_at": (
                        r["revoked_at"].isoformat()
                        if r["revoked_at"] is not None
                        and hasattr(r["revoked_at"], "isoformat")
                        else r["revoked_at"]
                    ),
                    "last_used_at": (
                        r["last_used_at"].isoformat()
                        if r["last_used_at"] is not None
                        and hasattr(r["last_used_at"], "isoformat")
                        else r["last_used_at"]
                    ),
                    "notes": r["notes"],
                    "is_active": r["revoked_at"] is None,
                    "is_self": r["label"] == label,
                }
            )
    return templates.TemplateResponse(
        request,
        "admin_keys.html",
        {
            "items": rows,
            "current_actor": label,
        },
    )


@router.get("/me/credentials", response_class=HTMLResponse)
async def me_credentials_page(
    request: Request,
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """Self-service upstream-credentials dashboard (Plan v3 §B.7).

    Submitter+ may view, set, and delete THEIR OWN stored
    credentials. Viewer is intentionally blocked because a viewer
    cannot submit a session and storing a credential they can't
    use is meaningless.

    The page itself is read-only Jinja; mutations call
    ``/api/v1/me/credentials/{env_name}`` via HTMX so the API-side
    allowlist / encryption checks drive user-visible errors.
    """
    label = _dashboard_label(request)
    if not label:
        return HTMLResponse("Login required", status_code=401)
    role = _dashboard_role(request)
    if ROLE_HIERARCHY.get(role, 0) < ROLE_HIERARCHY["submitter"]:
        return HTMLResponse(
            "<div class='error'>Forbidden — submitter+ only.</div>",
            status_code=403,
        )
    store = getattr(request.app.state, "user_credentials_store", None)
    feature_disabled = store is None or not store.enabled
    rows: list[dict[str, Any]] = []
    if not feature_disabled:
        raw_rows = await store.list_for_user(label)
        # Plan v3 §B.7 follow-up — show each row's value as a
        # fixed-length mask so the user can tell their own keys apart
        # without ever echoing back plaintext. Bricked rows return no
        # plaintext from `get_for_user` (they are skipped); render an
        # explicit "(bricked)" so the row is still understandable.
        plaintexts = await store.get_for_user(label)
        for r in raw_rows:
            env = r["env_name"]
            pt = plaintexts.get(env)
            rows.append(
                {
                    "env_name": env,
                    "value_masked": (
                        mask_credential_value(pt)
                        if pt is not None
                        else "(bricked)"
                    ),
                    "is_bricked": pt is None,
                    "updated_at": (
                        r["updated_at"].isoformat()
                        if hasattr(r["updated_at"], "isoformat")
                        else r["updated_at"]
                    ),
                    "created_by_label": r["created_by_label"],
                    "notes": r["notes"],
                }
            )
    return templates.TemplateResponse(
        request,
        "me_credentials.html",
        {
            "credentials": rows,
            "current_actor": label,
            "feature_disabled": feature_disabled,
            "allowed_env_names": sorted(_CRED_ALLOWED_ENV_NAMES),
            "active_nav": "me_credentials",
        },
    )


@router.get("/admin/credentials", response_class=HTMLResponse)
async def admin_credentials_page(
    request: Request,
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """Operator view of every user's credentials (Plan v3 §B.7).

    Admin-only. Identity / role resolution mirrors
    :func:`admin_keys_page` so the legacy-admin → admin fallback in
    :func:`_dashboard_role` keeps working (don't regress the
    user-reported 403 bug from Plan v3 prep).
    """
    label = _dashboard_label(request)
    if not label:
        return HTMLResponse("Login required", status_code=401)
    role = _dashboard_role(request)
    if ROLE_HIERARCHY.get(role, 0) < ROLE_HIERARCHY["admin"]:
        return HTMLResponse(
            "<div class='error'>Forbidden — admin only.</div>",
            status_code=403,
        )
    # Plan v3 §B.7 follow-up — admin can narrow the view to a single
    # user_label via ?user_label=alice. Empty / missing → show all.
    # The filter is purely a render-side projection; the table still
    # uses ``store.list_all()`` so the datalist always has the full
    # set of known users to switch to. Trim only — exact match
    # against the stored ``user_label`` column.
    user_label_filter = (request.query_params.get("user_label") or "").strip()
    store = getattr(request.app.state, "user_credentials_store", None)
    feature_disabled = store is None or not store.enabled
    rows: list[dict[str, Any]] = []
    bricked_rows: list[dict[str, Any]] = []
    user_label_choices: list[str] = []
    if not feature_disabled:
        raw_rows = await store.list_all()
        # Per-user plaintext map cached so we don't decrypt twice when
        # the same user has multiple env_name rows. Each lookup is
        # one DB roundtrip + N Fernet decrypts; N here is tiny
        # (≤ |ALLOWED_ENV_NAMES| = 10) so caching by user is enough.
        plaintext_cache: dict[str, dict[str, str]] = {}
        for r in raw_rows:
            ul = r["user_label"]
            if user_label_filter and ul != user_label_filter:
                continue
            if ul not in plaintext_cache:
                plaintext_cache[ul] = await store.get_for_user(ul)
            pt = plaintext_cache[ul].get(r["env_name"])
            rows.append(
                {
                    "user_label": ul,
                    "env_name": r["env_name"],
                    "value_masked": (
                        mask_credential_value(pt)
                        if pt is not None
                        else "(bricked)"
                    ),
                    "is_bricked": pt is None,
                    "updated_at": (
                        r["updated_at"].isoformat()
                        if hasattr(r["updated_at"], "isoformat")
                        else r["updated_at"]
                    ),
                    "created_by_label": r["created_by_label"],
                    "notes": r["notes"],
                }
            )
        for r in await store.list_bricked():
            ul = r["user_label"]
            if user_label_filter and ul != user_label_filter:
                continue
            bricked_rows.append(
                {
                    "user_label": ul,
                    "env_name": r["env_name"],
                    "key_fingerprint": r["key_fingerprint"],
                    "updated_at": (
                        r["updated_at"].isoformat()
                        if hasattr(r["updated_at"], "isoformat")
                        else r["updated_at"]
                    ),
                }
            )
        # Plan v3 §B.7 follow-up — datalist candidates: union of
        # users with stored rows + configured dashboard cookie users
        # + role-mapping labels + the legacy admin label. The input
        # is still free-form so admin can pre-seed credentials for a
        # user_label that hasn't logged in yet.
        cfg = request.app.state.config
        candidates: set[str] = {r["user_label"] for r in raw_rows}
        candidates.update(
            f"dashboard-{u}"
            for u in (getattr(cfg, "dashboard_users", {}) or {})
        )
        candidates.update(getattr(cfg, "role_mapping", {}) or {})
        candidates.add("dashboard-admin")
        user_label_choices = sorted(candidates)

    return templates.TemplateResponse(
        request,
        "admin_credentials.html",
        {
            "credentials": rows,
            "bricked_credentials": bricked_rows,
            "current_actor": label,
            "feature_disabled": feature_disabled,
            "allowed_env_names": sorted(_CRED_ALLOWED_ENV_NAMES),
            "active_nav": "admin_credentials",
            "user_label_filter": user_label_filter,
            "user_label_choices": user_label_choices,
        },
    )


def _admin_creds_cell_guard(
    request: Request,
) -> tuple[str, Any] | HTMLResponse:
    """Common admin / feature-enabled / env-name guard for the
    reveal-cell + mask-cell fragment endpoints.

    Returns ``(label, store)`` on success, or a pre-built
    :class:`HTMLResponse` (401 / 403 / 503) on rejection. Centralises
    the three failure modes so both fragment routes stay one short
    function each.
    """
    label = _dashboard_label(request)
    if not label:
        return HTMLResponse(
            "<span class='error'>Login required</span>", status_code=401
        )
    role = _dashboard_role(request)
    if ROLE_HIERARCHY.get(role, 0) < ROLE_HIERARCHY["admin"]:
        return HTMLResponse(
            "<span class='error'>Forbidden — admin only.</span>",
            status_code=403,
        )
    store = getattr(request.app.state, "user_credentials_store", None)
    if store is None or not store.enabled:
        return HTMLResponse(
            "<span class='error'>Feature disabled.</span>",
            status_code=503,
        )
    return label, store


def _admin_creds_mask_button(user: str, env: str, masked: str) -> str:
    """HTML fragment for the default (mask + Reveal) admin cell.

    Used both by ``admin_credentials.html`` initial render and by the
    Hide-button swap target so the two paths can never drift in
    formatting.
    """
    safe_user = urllib.parse.quote(user, safe="")
    safe_env = urllib.parse.quote(env, safe="")
    from html import escape as html_escape

    return (
        f'<code class="cred-mask" '
        f'title="Last 4 chars only — full value is never echoed back.">'
        f"{html_escape(masked)}</code> "
        f'<button type="button" class="reveal-btn" '
        f'hx-get="/dashboard/admin/credentials/cell/reveal'
        f"?user={safe_user}&env={safe_env}\" "
        f'hx-target="closest td" hx-swap="innerHTML" '
        f'hx-confirm="Reveal plaintext for '
        f'{html_escape(user)} / {html_escape(env)}? '
        f'This action is logged.">Reveal</button>'
    )


@router.get(
    "/admin/credentials/cell/reveal", response_class=HTMLResponse
)
async def admin_credentials_reveal_cell(
    request: Request,
    user: str,
    env: str,
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """HTMX fragment: return plaintext + Hide button for ONE row.

    Plan v3 §B.7 follow-up — every successful reveal writes one
    ``user_credential_admin_reveal`` audit row before the response is
    rendered (metadata-only — never the plaintext). 4xx paths
    intentionally do NOT audit so failed admin probes don't pollute
    the log; the dashboard's auth/role check upstream already gates
    who can hit this endpoint at all.
    """
    guard = _admin_creds_cell_guard(request)
    if isinstance(guard, HTMLResponse):
        return guard
    label, store = guard
    if env not in _CRED_ALLOWED_ENV_NAMES:
        return HTMLResponse(
            "<span class='error'>env_name not allowed</span>",
            status_code=400,
        )

    plaintexts = await store.get_for_user(user)
    value = plaintexts.get(env)
    if value is None:
        return HTMLResponse(
            "<span class='error'>not found / bricked</span>",
            status_code=404,
        )

    audit = getattr(request.app.state, "audit_service", None)
    if audit is not None:
        await audit.record(
            actor=label,
            action="user_credential_admin_reveal",
            target_type="user_credential",
            target_id=f"{user}/{env}",
            metadata={
                "env_name": env,
                "value_length_class": _cred_length_class(value),
                "victim_label": user,
                "self_service": False,
            },
        )

    from html import escape as html_escape

    safe_user = urllib.parse.quote(user, safe="")
    safe_env = urllib.parse.quote(env, safe="")
    return HTMLResponse(
        f'<code class="cred-plaintext">{html_escape(value)}</code> '
        f'<button type="button" class="hide-btn" '
        f'hx-get="/dashboard/admin/credentials/cell/mask'
        f"?user={safe_user}&env={safe_env}\" "
        f'hx-target="closest td" hx-swap="innerHTML">Hide</button>'
    )


@router.get(
    "/admin/credentials/cell/mask", response_class=HTMLResponse
)
async def admin_credentials_mask_cell(
    request: Request,
    user: str,
    env: str,
    _: None = _RequireSessionDep,
) -> HTMLResponse:
    """HTMX fragment: return mask + Reveal button (the default cell
    state). Used by the Hide button to roll back to the safe view
    without forcing a full page reload. NOT audited — this endpoint
    never returns plaintext."""
    guard = _admin_creds_cell_guard(request)
    if isinstance(guard, HTMLResponse):
        return guard
    _, store = guard
    if env not in _CRED_ALLOWED_ENV_NAMES:
        return HTMLResponse(
            "<span class='error'>env_name not allowed</span>",
            status_code=400,
        )

    plaintexts = await store.get_for_user(user)
    value = plaintexts.get(env)
    if value is None:
        # Bricked or deleted between reveal/hide — render the same
        # warning marker the page-level render would have shown.
        from html import escape as html_escape

        return HTMLResponse(
            f'<span class="badge" '
            f'style="background:#fef3c7;color:#b45309;" '
            f'title="Encrypted with a previous '
            f'RELAY_CREDENTIALS_ENCRYPTION_KEY; re-enter to fix.">'
            f"{html_escape('bricked')}</span>"
        )

    return HTMLResponse(
        _admin_creds_mask_button(user, env, mask_credential_value(value))
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
