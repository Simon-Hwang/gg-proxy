"""``/api/v1/sessions/{id}/hitl`` HITL endpoints.

Two responsibilities:
- list pending HITL requests for a session;
- resolve a single request by ``req_id`` (accept/deny + reason).
"""
from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from gg_relay.api.deps import CoordinatorDep, StoreDep
from gg_relay.api.schemas import (
    HITLPendingItem,
    HITLPendingResponse,
    HITLResolveRequest,
)
from gg_relay.session.hitl.coordinator import HITLCoordinator, HITLNotPending
from gg_relay.store import SessionRepository

router = APIRouter(prefix="/sessions/{session_id}/hitl", tags=["hitl"])


@router.get("/pending", response_model=HITLPendingResponse)
async def list_pending(
    session_id: str,
    coordinator: HITLCoordinator = CoordinatorDep,
) -> HITLPendingResponse:
    snap = coordinator.pending_snapshot(session_id=session_id)
    return HITLPendingResponse(
        session_id=session_id,
        pending=[
            HITLPendingItem(req_id=rid, tool=v["tool"], args=v["args"])
            for rid, v in snap.items()
        ],
    )


@router.post("/{req_id}", status_code=200)
async def resolve(
    session_id: str,
    req_id: str,
    body: HITLResolveRequest,
    coordinator: HITLCoordinator = CoordinatorDep,
    store: SessionRepository = StoreDep,
) -> dict[str, str]:
    full_req_id = req_id if ":" in req_id else f"{session_id}:{req_id}"
    reason = body.reason
    if body.resolver:
        reason = f"{reason}|by:{body.resolver}" if reason else f"by:{body.resolver}"
    try:
        await coordinator.resolve(full_req_id, body.decision, reason=reason)
    except HITLNotPending as exc:
        raise HTTPException(
            status_code=409, detail="hitl already resolved or unknown"
        ) from exc
    # Persist the decision (defensive — the manager also writes the frame).
    # Persistence is best-effort; the in-memory coordinator owns the
    # authoritative resolution state.
    with contextlib.suppress(Exception):
        await store.upsert_hitl(
            id=full_req_id,
            session_id=session_id,
            tool="",
            args_json={},
            status=body.decision,
            resolved_at=datetime.now(UTC),
            reason=body.reason,
            resolver=body.resolver,
        )
    return {"status": body.decision, "req_id": full_req_id}
