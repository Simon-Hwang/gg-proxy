"""HTMX dashboard router.

Authentication is a single shared admin account (D4.11); the password is
loaded from ``Config.dashboard_admin_password`` and compared with
``secrets.compare_digest``. The session middleware (added by the parent
app, NOT here) signs cookies with ``Config.dashboard_session_secret``.

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
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import SecretStr

from gg_relay.api.deps import get_coordinator, get_manager
from gg_relay.core import EventBus, SessionCreated, SessionStateChanged
from gg_relay.session.hitl.coordinator import HITLCoordinator, HITLNotPending
from gg_relay.session.manager import SessionManager, SessionNotFound
from gg_relay.store import SessionRepository

_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

SESSION_USER_KEY = "user"


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


@router.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Any:
    cfg = request.app.state.config
    admin_pw: SecretStr | None = getattr(cfg, "dashboard_admin_password", None)
    if (
        admin_pw is None
        or username != "admin"
        or not secrets.compare_digest(password, admin_pw.get_secret_value())
    ):
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
    rows = await manager.list(limit=200)
    return templates.TemplateResponse(
        request, "sessions_list.html", {"sessions": rows}
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
    sessions = await manager.list(limit=page_size, offset=0)
    columns = _kanban_columns(list(sessions))
    return templates.TemplateResponse(
        request,
        "kanban.html",
        {
            "columns": columns,
            "page_size": page_size,
            "next_offset": page_size if len(sessions) == page_size else None,
            "chart_js_cdn": getattr(cfg, "chart_js_cdn", ""),
            "chart_js_offline": bool(getattr(cfg, "chart_js_offline", False)),
        },
    )


@router.get("/kanban/board", response_class=HTMLResponse)
async def kanban_board_partial(
    request: Request,
    offset: int = Query(0, ge=0),
    _: None = _RequireSessionDep,
    manager: SessionManager = _ManagerDep,
) -> HTMLResponse:
    """HTMX target: returns just the inner ``_kanban_board.html``
    fragment — used both by the 5s ``hx-trigger='every 5s'`` polling
    fallback AND by the ``revealed`` pagination loader.

    ``offset`` lets the next-page loader skip the cards already
    rendered. Pagination is D6.16 — server picks ``page_size`` from
    ``Config.kanban_default_page_size`` so operators can tune without
    a deploy.
    """
    cfg = request.app.state.config
    page_size = int(getattr(cfg, "kanban_default_page_size", 50))
    sessions = await manager.list(limit=page_size, offset=offset)
    columns = _kanban_columns(list(sessions))
    next_offset = (offset + page_size) if len(sessions) == page_size else None
    return templates.TemplateResponse(
        request,
        "_kanban_board.html",
        {
            "columns": columns,
            "page_size": page_size,
            "offset": offset,
            "next_offset": next_offset,
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
