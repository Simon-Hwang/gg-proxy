"""``/api/v1/sessions`` REST endpoints.

The router translates :class:`SessionSubmitRequest` → SessionSpec +
SessionRuntimeContext, calls :class:`SessionManager`, and adapts the
returned :class:`SessionDetail` to :class:`SessionResponse`. Credentials
are PoP: present in the request body, consumed by the manager via the
runtime context, never serialised back out.
"""
from __future__ import annotations

import contextlib
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from gg_relay.api.dependencies.require_role import (
    ROLE_HIERARCHY,
    _resolve_role,
    require_role,
    require_role_or_own_session,
)
from gg_relay.api.deps import ApiKeyIdDep, ManagerDep
from gg_relay.api.schemas import (
    BatchSessionItem,
    BatchSessionRequest,
    BatchSessionResponse,
    CancelRequest,
    FrameOut,
    PauseRequest,
    ResumeRequest,
    SearchSessionItem,
    SearchSessionsResponse,
    SessionDetailResponse,
    SessionListResponse,
    SessionResponse,
    SessionSubmitRequest,
)
from gg_relay.core import RetryConfigError, SDKError, SessionState
from gg_relay.session.manager import (
    MaxPausedExceeded,
    ResumeQueueTimeout,
    SessionDetail,
    SessionManager,
    SessionNotFound,
    SessionNotPaused,
    SessionNotRunning,
)
from gg_relay.session.runner.bridge import BridgeAckTimeout
from gg_relay.session.spec import (
    PluginManifest,
    SessionRuntimeContext,
    SessionSpec,
)
from gg_relay.store import (
    ConcurrencyError,
    CursorFilterMismatchError,
    CursorInvalidError,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _build_spec(req: SessionSubmitRequest) -> SessionSpec:
    plugins = PluginManifest(
        profile=req.spec.plugins.profile,
        modules=tuple(req.spec.plugins.modules),
        skills=tuple(req.spec.plugins.skills),
        with_components=tuple(req.spec.plugins.with_components),
        without_components=tuple(req.spec.plugins.without_components),
        extra_env=tuple((k, v) for k, v in req.spec.plugins.extra_env),
    )
    return SessionSpec(
        prompt=req.spec.prompt,
        cwd=Path(req.spec.cwd),
        plugins=plugins,
        executor=req.spec.executor,
        timeout_s=req.spec.timeout_s,
        tags=tuple(req.spec.tags),
    )


def _detail_to_response(detail: SessionDetail) -> SessionDetailResponse:
    return SessionDetailResponse(
        id=detail.id,
        status=detail.status.value,
        spec=detail.spec_json,
        tags=list(detail.tags),
        submitted_at=detail.submitted_at,
        started_at=detail.started_at,
        ended_at=detail.ended_at,
        end_reason=detail.end_reason,
        backend=detail.backend,
        trace_id=detail.trace_id,
        runtime_id=detail.runtime_id,
        owner=detail.owner,
        description=detail.description,
        frames=[
            FrameOut(
                seq=int(f.get("seq", 0)),
                ts=f["ts"],
                type=str(f.get("type", "")),
                payload=dict(f.get("payload") or {}),
            )
            for f in detail.frames
        ],
    )


# Plan 7 Task 6b / D7.26 — defensive truncation cap. The
# :class:`SessionSubmitRequest` schema already enforces
# ``max_length=512`` via pydantic so a well-formed request never
# trips this branch; it stays as a belt-and-braces fallback for
# in-process callers that construct the model with
# :meth:`pydantic.BaseModel.model_construct` (which bypasses
# validators) and a future-proof guard if the schema cap is loosened
# in a later release.
_DESCRIPTION_MAX_LEN = 512


@router.post(
    "",
    response_model=SessionResponse,
    status_code=202,
    dependencies=[Depends(require_role("submitter"))],
)
async def submit_session(
    request: Request,
    body: SessionSubmitRequest,
    manager: SessionManager = ManagerDep,
    api_key_id: str | None = ApiKeyIdDep,
) -> JSONResponse:
    """Submit a new session.

    Plan 7 Task 6b / D7.26 — collaboration metadata flow:

      1. ``owner`` resolution: ``body.owner`` (operator override) →
         ``request.state.api_key_label`` (auto-attribute from
         API-key middleware) → ``"anon"`` (default for un-authed
         test paths). The router does this collapse so the
         :class:`SessionManager` never has to reach into Starlette
         state — keeping the manager framework-agnostic.
      2. ``description`` truncation: schema cap is 512 chars; we
         apply a defensive in-place truncation on the response
         path and emit ``X-Description-Truncated: true`` whenever
         truncation actually happened.
    """
    spec = _build_spec(body)
    ctx = SessionRuntimeContext(
        credentials=dict(body.credentials),
        trace_id=body.trace_id or "",
    )
    # Resolve owner — router-owned (manager intentionally does not
    # read ``request.state``). ``getattr`` guards the case where the
    # APIKey middleware wasn't wired (e.g. tests with
    # ``allow_no_keys=True``).
    owner = (
        body.owner
        or getattr(request.state, "api_key_label", None)
        or "anon"
    )
    description = body.description
    response_headers: dict[str, str] = {}
    if description is not None and len(description) > _DESCRIPTION_MAX_LEN:
        description = description[:_DESCRIPTION_MAX_LEN]
        response_headers["X-Description-Truncated"] = "true"
    try:
        sid = await manager.submit(
            spec,
            runtime_ctx=ctx,
            api_key_id=api_key_id,
            owner=owner,
            description=description,
        )
    except SDKError as exc:
        # Plan 7 D7.25 / Task 14 — typed SDK errors carry their own
        # HTTP status + machine-readable ``error_category``. We
        # surface both so dashboards can render an actionable message
        # without parsing the bare exception class name.
        raise HTTPException(
            status_code=exc.http_status,
            detail={
                "code": f"sdk_{exc.category}",
                "error_category": exc.category,
                "message": str(exc),
            },
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    detail = await manager.get(sid)
    payload = SessionResponse(
        id=detail.id,
        status=detail.status.value,
        spec=detail.spec_json,
        tags=list(detail.tags),
        submitted_at=detail.submitted_at,
        started_at=detail.started_at,
        ended_at=detail.ended_at,
        end_reason=detail.end_reason,
        backend=detail.backend,
        trace_id=detail.trace_id,
        owner=detail.owner,
        description=detail.description,
    )
    return JSONResponse(
        payload.model_dump(mode="json"),
        status_code=202,
        headers=response_headers,
    )


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    status: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    after: str | None = Query(default=None),
    manager: SessionManager = ManagerDep,
) -> Any:
    """List sessions newest-first with cursor pagination.

    Plan 7 D7.6 / Task 9. Pass ``after=<next_cursor>`` from a previous
    response to fetch the next page; ``limit`` is capped at 100 to keep
    page sizes bounded. The response body carries both the new
    cursor-shaped fields (``items`` + ``next_cursor``) and the
    deprecated v0.6 fields (``sessions`` alias + ``total=-1`` sentinel)
    so existing clients keep working until 0.8.0.

    Error mapping — body is a flat ``{"detail": msg, "code": ...}`` JSON
    object (no nesting) so machine clients can dispatch on ``code``:
      * 400 ``invalid_status``           — unknown ``status`` value
      * 400 ``cursor_invalid``           — malformed ``after`` token
      * 400 ``cursor_filter_mismatch``   — cursor was minted under a
        different ``status`` / ``tag`` combination than the current
        query (re-mint by dropping ``after`` and starting over)
    """
    state: SessionState | None = None
    if status:
        try:
            state = SessionState(status)
        except ValueError:
            return JSONResponse(
                {
                    "detail": f"invalid status: {status!r}",
                    "code": "invalid_status",
                },
                status_code=400,
            )
    try:
        rows, next_cursor = await manager.list(
            status=state, tag=tag, limit=limit, after=after
        )
    except CursorFilterMismatchError as exc:
        return JSONResponse(
            {"detail": str(exc), "code": "cursor_filter_mismatch"},
            status_code=400,
        )
    except CursorInvalidError as exc:
        return JSONResponse(
            {"detail": str(exc), "code": "cursor_invalid"},
            status_code=400,
        )
    items = [
        SessionResponse(
            id=r.id,
            status=r.status.value,
            spec={},
            tags=list(r.tags),
            submitted_at=r.submitted_at,
            started_at=r.started_at,
            ended_at=r.ended_at,
            end_reason=r.end_reason,
            backend=r.backend,
        )
        for r in rows
    ]
    return SessionListResponse(
        items=items,
        next_cursor=next_cursor,
        sessions=items,
        total=-1,
    )


# ── Plan 8 D8.20 / Task 12 — session search endpoint ─────────────────
# IMPORTANT: ``/search`` is a *literal* path segment that must be
# declared BEFORE ``/{session_id}`` — FastAPI matches routes in
# declaration order, and a path-param route declared first would
# swallow ``GET /sessions/search`` as ``session_id='search'``.


@router.get("/search", response_model=SearchSessionsResponse)
async def search_sessions(
    request: Request,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    owner: Annotated[str | None, Query(max_length=64)] = None,
    tags: Annotated[list[str] | None, Query()] = None,
    status: Annotated[list[str] | None, Query()] = None,
    after_ts: Annotated[datetime | None, Query()] = None,
    before_ts: Annotated[datetime | None, Query()] = None,
    after: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> SearchSessionsResponse:
    """Search sessions with combined filters + cursor pagination.

    Plan 8 D8.20 / Task 12. Backed by
    :meth:`SqlAlchemyStore.search_sessions` — see that docstring for
    the filter / cursor contract. Filters compose with AND across
    kwargs and OR within ``tags`` / ``status`` lists. ``q`` matches
    against the JSON spec payload (where the prompt lives) so search
    works for both legacy rows (no top-level prompt column) and new
    rows alike.

    RBAC (inline, same pattern as ``GET /audit``): admin sees every
    row; submitter / viewer are silently force-filtered to
    ``owner=<self-label>``. An explicit cross-owner filter from a
    non-admin caller surfaces ``403 forbidden_search_owner`` rather
    than a silent rewrite — explicit-deny avoids the confusing "I
    asked for bob's rows but got mine" response shape.

    Error mapping:
      * 400 ``cursor_invalid`` / ``cursor_filter_mismatch`` — same
        contract as :func:`list_sessions`.
      * 403 ``forbidden_search_owner`` — non-admin asked for an owner
        they don't match.
    """
    store = request.app.state.store
    label = getattr(request.state, "api_key_label", None)
    role = _resolve_role(request)

    if ROLE_HIERARCHY.get(role, 0) < ROLE_HIERARCHY["admin"]:
        if owner is not None and owner != label:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "cannot search by other owner",
                    "code": "forbidden_search_owner",
                    "required_role": "admin",
                    "current_role": role,
                },
            )
        owner = label

    try:
        rows, next_cursor = await store.search_sessions(
            q=q,
            owner=owner,
            tags=tags,
            status=status,
            after_ts=after_ts,
            before_ts=before_ts,
            after=after,
            limit=limit,
        )
    except CursorFilterMismatchError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": str(exc),
                "code": "cursor_filter_mismatch",
            },
        ) from exc
    except CursorInvalidError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "code": "cursor_invalid"},
        ) from exc

    items: list[SearchSessionItem] = []
    for r in rows:
        spec = r.get("spec_json") if hasattr(r, "get") else None
        prompt_val = ""
        if isinstance(spec, dict):
            prompt_val = str(spec.get("prompt") or "")
        items.append(
            SearchSessionItem(
                id=r["id"],
                prompt=prompt_val,
                owner=r.get("owner") if hasattr(r, "get") else None,
                description=(
                    r.get("description") if hasattr(r, "get") else None
                ),
                status=r["status"],
                tags=list(r["tags"] or []),
                submitted_at=r["submitted_at"],
                ended_at=r.get("ended_at") if hasattr(r, "get") else None,
            )
        )
    return SearchSessionsResponse(
        items=items,
        next_cursor=next_cursor,
        has_more=next_cursor is not None,
    )


# ── Plan 8 D8.21 / Task 13: per-user session favorites ─────────────
#
# ``GET /sessions/favorites`` MUST be declared BEFORE the
# ``GET /sessions/{session_id}`` route below — FastAPI matches in
# registration order, and a literal sub-path ("favorites") only wins
# over a path parameter ("{session_id}") when the literal is
# registered first. The star / un-star endpoints further down use a
# distinct method (POST / DELETE) so they don't collide with
# ``GET /{session_id}`` regardless of order.


@router.get("/favorites")
async def list_my_favorites(
    request: Request,
    user: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """List the caller's starred sessions (Plan 8 D8.21 / Task 13).

    Returns ``{"items": [...], "user": "<resolved label>"}`` sorted
    most-recently starred first. Each item carries enough session
    metadata (``prompt``, ``owner``, ``status``) for the dashboard's
    ``My Favorites`` table to render without a follow-up
    ``GET /sessions/{sid}`` round-trip.

    Role policy:
      * **non-admin** — sees only their own favorites; the ``user``
        query parameter is silently ignored.
      * **admin** — may pass ``?user=<label>`` to inspect another
        user's favorites (debugging / moderation).
    """
    store = request.app.state.store
    label = getattr(request.state, "api_key_label", None) or "anon"
    current_role = _resolve_role(request)
    is_admin = (
        ROLE_HIERARCHY.get(current_role, 0) >= ROLE_HIERARCHY["admin"]
    )
    target = user if (user and is_admin) else label
    rows = await store.list_favorites(user_label=target, limit=limit)
    items: list[dict[str, Any]] = []
    for r in rows:
        s = r["session"]
        starred_at = r["starred_at"]
        items.append(
            {
                "favorite_id": r["favorite_id"],
                "session_id": r["session_id"],
                "starred_at": (
                    starred_at.isoformat()
                    if hasattr(starred_at, "isoformat")
                    else starred_at
                ),
                "prompt": (s.get("spec_json") or {}).get("prompt", ""),
                "owner": s.get("owner"),
                "status": s.get("status"),
            }
        )
    return {"items": items, "user": target}


@router.get("/{session_id}", response_model=SessionDetailResponse)
async def get_session(
    session_id: str,
    frames_limit: int = Query(default=100, ge=1, le=1000),
    frames_offset: int = Query(default=0, ge=0),
    manager: SessionManager = ManagerDep,
) -> SessionDetailResponse:
    try:
        detail = await manager.get(
            session_id, frames_limit=frames_limit, frames_offset=frames_offset
        )
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    return _detail_to_response(detail)


@router.post(
    "/{session_id}/favorite",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role("submitter"))],
)
async def star_session(request: Request, session_id: str) -> None:
    """Star a session — idempotent (Plan 8 D8.21 / Task 13).

    Returns 204 on success. ``submitter+`` may star any session
    (favorites are a per-user view, not an ownership-gated action).

    Idempotency: a second star on the same session is a no-op and
    does NOT write a second ``session_star`` audit row — the
    repository signals "no state change" via ``add_favorite=False``
    and we skip the audit call so the timeline stays clean.

    Error mapping:
      * 404 ``session_not_found`` — unknown id; structured detail
        so machine clients can dispatch on ``code``.
    """
    store = request.app.state.store
    audit = getattr(request.app.state, "audit_service", None)
    label = getattr(request.state, "api_key_label", None) or "anon"

    sess = await store.get_session(session_id)
    if sess is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "session not found",
                "code": "session_not_found",
            },
        )

    added = await store.add_favorite(
        session_id=session_id, user_label=label
    )
    if added and audit is not None:
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            await audit.record(
                actor=label,
                action="session_star",
                target_type="session",
                target_id=session_id,
            )
    return None


@router.delete(
    "/{session_id}/favorite",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role("submitter"))],
)
async def unstar_session(request: Request, session_id: str) -> None:
    """Un-star a session — idempotent (Plan 8 D8.21 / Task 13).

    Returns 204 regardless of whether the row existed; un-starring
    a session that wasn't starred is a no-op. The audit row only
    fires on actual state change.
    """
    store = request.app.state.store
    audit = getattr(request.app.state, "audit_service", None)
    label = getattr(request.state, "api_key_label", None) or "anon"

    removed = await store.remove_favorite(
        session_id=session_id, user_label=label
    )
    if removed and audit is not None:
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            await audit.record(
                actor=label,
                action="session_unstar",
                target_type="session",
                target_id=session_id,
            )
    return None


@router.post(
    "/{session_id}/cancel",
    status_code=202,
    dependencies=[Depends(require_role_or_own_session("admin"))],
)
async def cancel_session(
    session_id: str,
    body: CancelRequest | None = None,
    manager: SessionManager = ManagerDep,
) -> dict[str, str]:
    reason = body.reason if body is not None else "user_request"
    try:
        await manager.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    await manager.cancel(session_id, reason=reason)
    return {"status": "cancelled", "session_id": session_id, "reason": reason}


# ── pause / resume / DELETE (Plan 6 Task 4 / D6.7 / D6.9 / D6.17) ──

# Retry-After hint when the global / per-key paused cap is reached.
# A small value (5s) is reasonable: paused sessions either resume
# quickly (operator click) or hit the paused-timeout (~30 min default).
_RETRY_AFTER_DEFAULT_S = 5


@router.post(
    "/{session_id}/pause",
    status_code=202,
    dependencies=[Depends(require_role_or_own_session("submitter"))],
)
async def pause_session(
    session_id: str,
    body: PauseRequest | None = None,
    manager: SessionManager = ManagerDep,
) -> JSONResponse:
    """Move a RUNNING session into the PAUSED state (Plan 6 D6.1/D6.2).

    Always returns 202 on success. Error mapping:
      * 404 — unknown id
      * 409 ``code=session_not_running``    — session not in RUNNING
      * 409 ``code=session_version_mismatch`` — Plan 7 D7.5: another
              writer mutated the row twice while pause was in flight
              (1 jitter retry exhausted); operator should refresh and
              retry
      * 429 — global or per-api-key paused cap exceeded; includes
              ``Retry-After`` header
      * 504 — runner didn't ack the pause within the bridge timeout
    """
    reason = body.reason if body is not None else None
    try:
        await manager.pause(session_id, reason=reason)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    except SessionNotRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MaxPausedExceeded as exc:
        return JSONResponse(
            {"detail": str(exc), "code": "max_paused_exceeded"},
            status_code=429,
            headers={"Retry-After": str(_RETRY_AFTER_DEFAULT_S)},
        )
    except BridgeAckTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except ConcurrencyError:
        return JSONResponse(
            {
                "detail": "session state changed, please refresh",
                "code": "session_version_mismatch",
            },
            status_code=409,
        )
    return JSONResponse(
        {"status": "paused", "session_id": session_id, "reason": reason or ""},
        status_code=202,
    )


@router.post(
    "/{session_id}/resume",
    status_code=202,
    dependencies=[Depends(require_role_or_own_session("submitter"))],
)
async def resume_session(
    session_id: str,
    body: ResumeRequest | None = None,
    manager: SessionManager = ManagerDep,
) -> JSONResponse:
    """Move a PAUSED session back to RUNNING (Plan 6 D6.2/D6.11).

    Error mapping:
      * 404 — unknown id
      * 409 ``code=session_not_paused``     — session not in PAUSED
      * 409 ``code=session_version_mismatch`` — Plan 7 D7.5: optimistic
              lock collision after 1 retry; operator should refresh
              and retry
      * 429 — couldn't re-acquire a semaphore slot within
              ``resume_timeout_s``; ``Retry-After`` advises when to retry
      * 504 — runner didn't ack the resume
    """
    hint = body.hint if body is not None else None
    try:
        await manager.resume(session_id, hint=hint)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    except SessionNotPaused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ResumeQueueTimeout as exc:
        return JSONResponse(
            {"detail": str(exc), "code": "resume_queue_timeout"},
            status_code=429,
            headers={"Retry-After": str(_RETRY_AFTER_DEFAULT_S)},
        )
    except BridgeAckTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except ConcurrencyError:
        return JSONResponse(
            {
                "detail": "session state changed, please refresh",
                "code": "session_version_mismatch",
            },
            status_code=409,
        )
    return JSONResponse(
        {"status": "running", "session_id": session_id, "hint": hint or ""},
        status_code=202,
    )


# ── Plan 8 D8.6 / Task 9 — batch cancel / retry ──────────────────────


@router.post(
    "/batch",
    response_model=BatchSessionResponse,
    status_code=200,
    dependencies=[Depends(require_role("submitter"))],
)
async def batch_sessions(
    request: Request,
    payload: BatchSessionRequest,
    manager: SessionManager = ManagerDep,
) -> BatchSessionResponse:
    """Bulk cancel or retry sessions (Plan 8 D8.6 / Task 9).

    Each id is processed in its own ``try``/``except`` so a single
    failure never blocks the rest of the batch — the response carries
    a per-id status array. Counts are precomputed in ``summary``.

    **Cancel** — admin OR own-session per id (the per-id ownership
    check mirrors :func:`require_role_or_own_session` used on the
    single-session endpoint). A submitter trying to cancel another
    user's session sees an ``error_code='forbidden_cancel'`` row,
    not a 403 for the whole batch — the action still succeeds for
    sessions they own. Sessions that don't exist surface as
    ``error_code='session_not_found'``.

    **Retry** — submitter+ for the WHOLE batch (the role guard at
    the route level handles this); each id calls
    :meth:`SessionManager.retry`, which copies the original spec
    and submits a new session whose ``parent_session_id`` points at
    the original. The new sid is returned as ``new_session_id`` so
    the dashboard can link to it. Original sessions in any
    terminal status (paused / failed / cancelled / completed) are
    eligible — retry doesn't enforce a status filter.

    Audit: every successful per-id action writes its own audit row
    (``session_batch_cancel`` for cancel; the retry path writes the
    standard ``session_retry`` action via
    :meth:`SessionManager.retry` itself, so we don't double-audit
    here). Per-id audit rows include the batch size in ``metadata``
    so the audit timeline can group by request.
    """
    store = request.app.state.store
    audit = request.app.state.audit_service
    label = getattr(request.state, "api_key_label", None) or "anon"
    role = _resolve_role(request)
    is_admin = ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY["admin"]
    batch_size = len(payload.ids)
    items: list[BatchSessionItem] = []
    ok_count = 0
    error_count = 0

    for sid in payload.ids:
        try:
            sess = await store.get_session(sid)
            if sess is None:
                items.append(
                    BatchSessionItem(
                        id=sid,
                        status="error",
                        error_code="session_not_found",
                        error_message=f"session {sid} not found",
                    )
                )
                error_count += 1
                continue

            if payload.action == "cancel":
                # Per-id ownership check — admin can cancel anything;
                # otherwise the session's owner must match the caller's
                # api_key_label.
                if not is_admin:
                    sess_owner = (
                        sess.get("owner") if hasattr(sess, "get") else None
                    )
                    if sess_owner != label:
                        items.append(
                            BatchSessionItem(
                                id=sid,
                                status="error",
                                error_code="forbidden_cancel",
                                error_message=(
                                    "not session owner; admin role required "
                                    "for cross-owner cancel"
                                ),
                            )
                        )
                        error_count += 1
                        continue
                await manager.cancel(
                    sid, reason=payload.reason or "batch_cancel"
                )
                # Plan 8 D8.4 — explicit batch-cancel audit row so the
                # audit timeline can group by ``session_batch_cancel``
                # without falling through to ``session_cancel`` (which
                # the manager already wrote inside ``cancel``).
                if audit is not None:
                    with contextlib.suppress(Exception):  # pragma: no cover - defensive
                        await audit.record(
                            actor=label,
                            action="session_batch_cancel",
                            target_type="session",
                            target_id=sid,
                            metadata={
                                "reason": payload.reason,
                                "batch_size": batch_size,
                            },
                        )
                items.append(BatchSessionItem(id=sid, status="ok"))
                ok_count += 1
            else:  # retry
                new_sid = await manager.retry(sid, actor=label)
                items.append(
                    BatchSessionItem(
                        id=sid, status="ok", new_session_id=new_sid
                    )
                )
                ok_count += 1
        except SessionNotFound:
            items.append(
                BatchSessionItem(
                    id=sid,
                    status="error",
                    error_code="session_not_found",
                    error_message=f"session {sid} not found",
                )
            )
            error_count += 1
        except RetryConfigError as exc:
            items.append(
                BatchSessionItem(
                    id=sid,
                    status="error",
                    error_code="retry_config_error",
                    error_message=str(exc),
                )
            )
            error_count += 1
        except SDKError as exc:
            items.append(
                BatchSessionItem(
                    id=sid,
                    status="error",
                    error_code=f"sdk_{exc.category}",
                    error_message=str(exc),
                )
            )
            error_count += 1
        except Exception as exc:  # pragma: no cover - defensive
            items.append(
                BatchSessionItem(
                    id=sid,
                    status="error",
                    error_code="internal_error",
                    error_message=str(exc),
                )
            )
            error_count += 1

    return BatchSessionResponse(
        items=items,
        summary={"ok": ok_count, "error": error_count},
    )


@router.delete(
    "/{session_id}",
    status_code=202,
    dependencies=[Depends(require_role_or_own_session("admin"))],
)
async def delete_session(
    session_id: str,
    manager: SessionManager = ManagerDep,
) -> dict[str, str]:
    """Idempotent cancel (Plan 6 D6.9=A).

    ``DELETE /sessions/{id}`` ≡ ``POST /sessions/{id}/cancel`` with an
    empty body. Always returns 202 — calling DELETE on an unknown or
    already-cancelled session is a no-op (we swallow
    :class:`SessionNotFound` rather than the standard 404 so clients
    can retry blindly on network flakes without special-casing the
    response).
    """
    # Idempotent — silently absorb the "already gone" case so clients
    # can retry blindly on network flakes without special-casing 404.
    with contextlib.suppress(SessionNotFound):
        await manager.cancel(session_id, reason="delete")
    return {
        "status": "cancelled",
        "session_id": session_id,
        "reason": "delete",
    }
