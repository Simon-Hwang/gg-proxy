"""``/api/v1/sessions/{id}/hitl`` HITL endpoints.

Two responsibilities:
- list pending HITL requests for a session;
- resolve a single request by ``req_id`` (accept/deny + reason).

Plan 7 D7.5 / Task 8 — resolve is the second optimistic-locking
checkpoint after :meth:`SessionManager.pause` / :meth:`resume`. Two
concurrent ``POST /sessions/{sid}/hitl/{req_id}`` calls now race
through:

  1. the in-process coordinator (:class:`HITLCoordinator`) which gates
     the future-completion at the asyncio level;
  2. the DB row via ``upsert_hitl(expected_version=...)`` which gates
     persistence in multi-worker deployments.

Both paths surface :class:`gg_relay.core.HITLAlreadyResolved` to the
router, which returns ``409`` with a body carrying the winning
decision so the loser sees what actually happened.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from gg_relay.api.deps import CoordinatorDep, StoreDep
from gg_relay.api.schemas import (
    HITLPendingItem,
    HITLPendingResponse,
    HITLResolveRequest,
)
from gg_relay.core import HITLAlreadyResolved
from gg_relay.session.hitl.coordinator import HITLCoordinator, HITLNotPending
from gg_relay.store import ConcurrencyError, SessionRepository

router = APIRouter(prefix="/sessions/{session_id}/hitl", tags=["hitl"])


def _decision_from_row(row: Any | None) -> dict[str, Any] | None:
    """Render a HITL DB row into the ``first_decision`` body fragment.

    Returns ``None`` when ``row`` is ``None`` (e.g. the in-memory race
    path where no DB write happened yet). ``resolved_at`` is
    isoformatted so the 409 body is plain JSON-friendly.
    """
    if row is None:
        return None
    resolved_at = row["resolved_at"]
    return {
        "status": row["status"],
        "resolver": row["resolver"],
        "reason": row["reason"],
        "resolved_at": (
            resolved_at.isoformat() if resolved_at is not None else None
        ),
    }


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
) -> Any:
    """Resolve a pending HITL request.

    Plan 7 D7.5 / Task 8 error mapping:

    * 200 — decision recorded.
    * 409 ``code=hitl_already_resolved`` — another caller won the race;
      response body contains ``first_decision`` with the winning
      ``status`` / ``resolver`` / ``reason`` / ``resolved_at`` (any of
      these may be ``null`` if the in-memory race path had no DB row
      to copy from).
    """
    full_req_id = req_id if ":" in req_id else f"{session_id}:{req_id}"
    reason = body.reason
    if body.resolver:
        reason = f"{reason}|by:{body.resolver}" if reason else f"by:{body.resolver}"

    # Read the current version BEFORE the coordinator handoff so the
    # subsequent upsert can detect a concurrent multi-worker writer.
    # Returns None when the row hasn't been written yet (Plan 4
    # coordinator-only path) — the upsert then skips the version
    # check and INSERTs fresh.
    expected_v = await store.get_hitl_version(full_req_id)
    pre_existing_row = await store.get_hitl(full_req_id)

    try:
        await coordinator.resolve(full_req_id, body.decision, reason=reason)
    except HITLNotPending:
        # In-process race — the other caller already drained the
        # future. Reach into the DB for the winning decision so the
        # loser's body is informative.
        winner_row = await store.get_hitl(full_req_id)
        return _conflict_response(full_req_id, winner_row)

    # Persist the decision with the version-checked upsert. A
    # cross-worker race (rare; covered for completeness) surfaces as
    # ConcurrencyError, which we map to the same 409 shape so the API
    # contract is uniform regardless of which layer caught the
    # conflict.
    try:
        await store.upsert_hitl(
            id=full_req_id,
            session_id=session_id,
            tool=pre_existing_row["tool"] if pre_existing_row else "",
            args_json=(
                dict(pre_existing_row["args_json"])
                if pre_existing_row
                else {}
            ),
            status=body.decision,
            created_at=(
                pre_existing_row["created_at"] if pre_existing_row else None
            ),
            resolved_at=datetime.now(UTC),
            reason=body.reason,
            resolver=body.resolver,
            expected_version=expected_v,
        )
    except ConcurrencyError:
        winner_row = await store.get_hitl(full_req_id)
        return _conflict_response(full_req_id, winner_row)

    return {"status": body.decision, "req_id": full_req_id}


def _conflict_response(req_id: str, winner_row: Any | None) -> JSONResponse:
    """Render the 409 ``hitl_already_resolved`` body.

    Body shape::

        {
            "detail": "HITL request {req_id} already resolved",
            "code": "hitl_already_resolved",
            "first_decision": {
                "status": "...",
                "resolver": "...",
                "reason": "...",
                "resolved_at": "<isoformat or null>"
            } | null
        }

    ``first_decision`` is ``null`` when the in-process race path had no
    DB row to copy from (Plan 4 coordinator-only flows).
    """
    err = HITLAlreadyResolved(
        req_id, first_decision=_decision_from_row(winner_row)
    )
    return JSONResponse(
        {
            "detail": str(err),
            "code": "hitl_already_resolved",
            "first_decision": err.first_decision,
        },
        status_code=409,
    )
