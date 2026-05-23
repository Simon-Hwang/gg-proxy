"""``/api/v1/sessions`` REST endpoints.

The router translates :class:`SessionSubmitRequest` → SessionSpec +
SessionRuntimeContext, calls :class:`SessionManager`, and adapts the
returned :class:`SessionDetail` to :class:`SessionResponse`. Credentials
are PoP: present in the request body, consumed by the manager via the
runtime context, never serialised back out.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from gg_relay.api.deps import ApiKeyIdDep, ManagerDep
from gg_relay.api.schemas import (
    CancelRequest,
    FrameOut,
    PauseRequest,
    ResumeRequest,
    SessionDetailResponse,
    SessionListResponse,
    SessionResponse,
    SessionSubmitRequest,
)
from gg_relay.core import SessionState
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


@router.post("", response_model=SessionResponse, status_code=202)
async def submit_session(
    request: SessionSubmitRequest,
    manager: SessionManager = ManagerDep,
    api_key_id: str | None = ApiKeyIdDep,
) -> SessionResponse:
    spec = _build_spec(request)
    ctx = SessionRuntimeContext(
        credentials=dict(request.credentials),
        trace_id=request.trace_id or "",
    )
    try:
        sid = await manager.submit(spec, runtime_ctx=ctx, api_key_id=api_key_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    detail = await manager.get(sid)
    return SessionResponse(
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


@router.post("/{session_id}/cancel", status_code=202)
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


@router.post("/{session_id}/pause", status_code=202)
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


@router.post("/{session_id}/resume", status_code=202)
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


@router.delete("/{session_id}", status_code=202)
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
