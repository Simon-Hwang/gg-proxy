"""``/api/v1/sessions`` REST endpoints.

The router translates :class:`SessionSubmitRequest` → SessionSpec +
SessionRuntimeContext, calls :class:`SessionManager`, and adapts the
returned :class:`SessionDetail` to :class:`SessionResponse`. Credentials
are PoP: present in the request body, consumed by the manager via the
runtime context, never serialised back out.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from gg_relay.api.deps import ManagerDep
from gg_relay.api.schemas import (
    CancelRequest,
    FrameOut,
    SessionDetailResponse,
    SessionListResponse,
    SessionResponse,
    SessionSubmitRequest,
)
from gg_relay.core import SessionState
from gg_relay.session.manager import (
    SessionDetail,
    SessionManager,
    SessionNotFound,
)
from gg_relay.session.spec import (
    PluginManifest,
    SessionRuntimeContext,
    SessionSpec,
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
    request: SessionSubmitRequest, manager: SessionManager = ManagerDep
) -> SessionResponse:
    spec = _build_spec(request)
    ctx = SessionRuntimeContext(
        credentials=dict(request.credentials),
        trace_id=request.trace_id or "",
    )
    try:
        sid = await manager.submit(spec, runtime_ctx=ctx)
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
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    manager: SessionManager = ManagerDep,
) -> SessionListResponse:
    state: SessionState | None = None
    if status:
        try:
            state = SessionState(status)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"invalid status: {status!r}"
            ) from exc
    rows = await manager.list(status=state, tag=tag, limit=limit, offset=offset)
    sessions = [
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
    return SessionListResponse(sessions=sessions, total=len(sessions))


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
