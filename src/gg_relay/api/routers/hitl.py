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

import contextlib
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from gg_relay.api.dependencies.require_role import require_role
from gg_relay.api.deps import CoordinatorDep, StoreDep
from gg_relay.api.schemas import (
    BatchHITLItem,
    BatchHITLRequest,
    BatchHITLResponse,
    HITLPendingItem,
    HITLPendingResponse,
    HITLResolveRequest,
)
from gg_relay.core import HITLAlreadyResolved
from gg_relay.session.hitl.coordinator import HITLCoordinator, HITLNotPending
from gg_relay.store import ConcurrencyError, SessionRepository

router = APIRouter(prefix="/sessions/{session_id}/hitl", tags=["hitl"])

# Plan 8 D8.6 / Task 9 — sibling router for the cross-session ``batch``
# endpoint. The per-session ``router`` above has a ``{session_id}`` path
# parameter that doesn't apply to a multi-session bulk action, so we
# expose batch under its own ``/hitl`` prefix and let ``api/main.py``
# include both routers under the same ``/api/v1`` umbrella.
batch_router = APIRouter(prefix="/hitl", tags=["hitl"])


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


@router.post(
    "/{req_id}",
    status_code=200,
    dependencies=[Depends(require_role("submitter"))],
)
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
    except HITLAlreadyResolved:
        # Plan 7 D7.20 / Task 14 — the coordinator's optional
        # ``store`` reference detected the row was already resolved
        # out-of-band (e.g. cross-worker race or direct
        # ``upsert_hitl`` from a job). Fetch a fresh row so the
        # response carries the most-recent winning decision.
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


# ── Plan 8 D8.6 / Task 9 — bulk approve / reject ─────────────────────


@batch_router.post(
    "/batch",
    response_model=BatchHITLResponse,
    status_code=200,
    dependencies=[Depends(require_role("submitter"))],
)
async def batch_hitl(
    request: Request,
    payload: BatchHITLRequest,
    coordinator: HITLCoordinator = CoordinatorDep,
    store: SessionRepository = StoreDep,
) -> BatchHITLResponse:
    """Bulk approve / reject HITL requests (Plan 8 D8.6 / Task 9).

    Each id is processed in its own ``try``/``except`` so a single
    failure never blocks the rest of the batch. The endpoint maps
    the user-facing ``approve``/``reject`` wording to the
    coordinator's internal ``accept``/``deny`` Literal, runs the
    same coordinator + store + audit pipeline as the single-resolve
    endpoint, and reports per-id status.

    Caller MUST supply the FULL HITL ids (``"{session_id}:{short}"``)
    as returned by ``GET /api/v1/sessions/{sid}/hitl/pending``; the
    batch endpoint does not auto-namespace because a batch typically
    spans multiple sessions.

    Error mapping (per id):

      * ``hitl_not_pending``      — request not currently pending.
      * ``hitl_already_resolved`` — DB row already has a winning
        decision (cross-worker race or post-resolve replay).
      * ``internal_error``        — anything else; ``error_message``
        carries the original exception's message.

    Audit: each successful resolve writes a ``hitl_batch_<action>``
    row (``approve`` / ``reject`` wording preserved so a dashboard
    audit timeline can group by user-facing intent rather than the
    internal accept/deny literal).
    """
    audit = request.app.state.audit_service
    label = getattr(request.state, "api_key_label", None) or "anon"
    decision: Literal["accept", "deny"] = (
        "accept" if payload.action == "approve" else "deny"
    )
    batch_size = len(payload.ids)
    items: list[BatchHITLItem] = []
    ok_count = 0
    error_count = 0

    for hid in payload.ids:
        try:
            # Record version + pre-existing row before flipping the
            # coordinator future, mirroring the single-resolve flow
            # (Plan 7 D7.5 optimistic-locking checkpoint).
            expected_v = await store.get_hitl_version(hid)
            pre_existing = await store.get_hitl(hid)

            await coordinator.resolve(hid, decision, reason=payload.reason)

            await store.upsert_hitl(
                id=hid,
                session_id=(
                    pre_existing["session_id"] if pre_existing else ""
                ),
                tool=pre_existing["tool"] if pre_existing else "",
                args_json=(
                    dict(pre_existing["args_json"])
                    if pre_existing
                    else {}
                ),
                status=decision,
                created_at=(
                    pre_existing["created_at"] if pre_existing else None
                ),
                resolved_at=datetime.now(UTC),
                reason=payload.reason,
                resolver=label,
                expected_version=expected_v,
            )
            if audit is not None:
                with contextlib.suppress(Exception):  # pragma: no cover - defensive
                    await audit.record(
                        actor=label,
                        action=f"hitl_batch_{payload.action}",
                        target_type="hitl",
                        target_id=hid,
                        metadata={
                            "reason": payload.reason,
                            "batch_size": batch_size,
                            "decision": decision,
                        },
                    )
            items.append(BatchHITLItem(id=hid, status="ok"))
            ok_count += 1
        except HITLAlreadyResolved as exc:
            items.append(
                BatchHITLItem(
                    id=hid,
                    status="error",
                    error_code="hitl_already_resolved",
                    error_message=str(exc),
                )
            )
            error_count += 1
        except HITLNotPending as exc:
            items.append(
                BatchHITLItem(
                    id=hid,
                    status="error",
                    error_code="hitl_not_pending",
                    error_message=str(exc) or hid,
                )
            )
            error_count += 1
        except ConcurrencyError as exc:
            items.append(
                BatchHITLItem(
                    id=hid,
                    status="error",
                    error_code="hitl_already_resolved",
                    error_message=str(exc),
                )
            )
            error_count += 1
        except Exception as exc:  # pragma: no cover - defensive
            items.append(
                BatchHITLItem(
                    id=hid,
                    status="error",
                    error_code="internal_error",
                    error_message=str(exc),
                )
            )
            error_count += 1

    return BatchHITLResponse(
        items=items,
        summary={"ok": ok_count, "error": error_count},
    )


def _conflict_response(req_id: str, winner_row: Any | None) -> JSONResponse:
    """Render the 409 ``hitl_already_resolved`` body.

    Body shape::

        {
            "detail": "HITL request {req_id} already resolved",
            "code": "hitl_already_resolved",
            "error_category": "hitl_already_resolved",
            "first_decision": {
                "status": "...",
                "resolver": "...",
                "reason": "...",
                "resolved_at": "<isoformat or null>"
            } | null
        }

    Plan 7 D7.25 / Task 14 — the ``error_category`` field mirrors the
    SDKError taxonomy contract on the sessions router so machine
    clients can dispatch on a uniform field across both paths.
    ``first_decision`` is ``null`` when the in-process race path had
    no DB row to copy from (Plan 4 coordinator-only flows).
    """
    err = HITLAlreadyResolved(
        req_id, first_decision=_decision_from_row(winner_row)
    )
    return JSONResponse(
        {
            "detail": str(err),
            "code": "hitl_already_resolved",
            "error_category": "hitl_already_resolved",
            "first_decision": err.first_decision,
        },
        status_code=409,
    )
