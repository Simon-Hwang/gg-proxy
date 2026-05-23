"""Audit listing endpoint — Plan 8 D8.4 / Task 6.

``GET /api/v1/audit`` exposes the durable audit log written by
:class:`gg_relay.api.audit_service.AuditService` for sensitive-mutation
trails (session submit / cancel / pause / resume / HITL / role
changes…). The route is read-only; audit *writes* happen exclusively
through the service (inline in mutation handlers OR the
:class:`gg_relay.api.middleware.audit.AuditFallbackMiddleware`) so a
compromised client cannot inject synthetic rows.

Cursor pagination reuses the Plan 7 D7.6 / Task 5 helpers
(:func:`gg_relay.store.repository._audit_filter_hash` /
:func:`_encode_audit_cursor`); paging across a filter change is
rejected with a clear 400 ``cursor_invalid`` rather than silently
returning a confusing mix of rows.

RBAC (inline — see "why not require_role" below):

* ``admin``                    — sees every row.
* ``submitter`` / ``viewer``   — when ``session_id`` is supplied they
  MUST own that session (else 403 ``forbidden_audit_view``); when
  ``session_id`` is omitted the ``actor`` filter is forced to the
  caller's own label so they cannot enumerate other operators'
  actions (an explicit ``actor`` that does NOT match their label
  → 403 ``forbidden_audit_filter``).

Why inline RBAC instead of the :func:`require_role` Depends:
:func:`require_role` issues a flat allow/deny on the role rank, but
the audit endpoint needs *filter override* semantics — when a viewer
asks ``GET /audit`` without ``session_id`` we want to silently rewrite
``actor=<their label>`` rather than 403. Mixing those two policies via
``Depends`` + a second post-filter pass risks the two layers
disagreeing on what "allowed" means (the dependency could pass while
the post-filter rewrites the response shape unpredictably). Keeping
the whole policy in one place makes the contract reviewable.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from gg_relay.api.dependencies.require_role import (
    ROLE_HIERARCHY,
    _resolve_role,
)
from gg_relay.store.exceptions import (
    CursorFilterMismatchError,
    CursorInvalidError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("")
async def list_audit_events(
    request: Request,
    session_id: Annotated[str | None, Query(max_length=64)] = None,
    actor: Annotated[str | None, Query(max_length=64)] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    target_type: Annotated[str | None, Query(max_length=32)] = None,
    target_id: Annotated[str | None, Query(max_length=128)] = None,
    after: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """List audit rows newest-first with cursor pagination.

    Response shape::

        {
            "items": [
                {
                    "id": <int>,
                    "ts": "<iso8601>",
                    "actor": "<label>",
                    "action": "<verb>",
                    "target_type": "session" | ... | null,
                    "target_id": "<sid>" | null,
                    "metadata": {...} | {},
                    "request_id": "<uuid>" | null
                },
                ...
            ],
            "next_cursor": "<opaque>" | null,
            "has_more": <bool>
        }

    ``has_more`` mirrors the truthiness of ``next_cursor`` (kept as a
    convenience field so dashboards don't have to do the ``is None``
    check themselves).
    """
    store = request.app.state.store

    label = getattr(request.state, "api_key_label", None)
    role = _resolve_role(request)

    if ROLE_HIERARCHY.get(role, 0) < ROLE_HIERARCHY["admin"]:
        if session_id is not None:
            # Filter-by-session path: viewer/submitter must own the
            # session. We surface 404 cleanly when the session genuinely
            # doesn't exist so the "not found" and "forbidden" branches
            # stay distinct (matches the require_role_or_own_session
            # contract — see Plan 8 Task 4 / D8.22).
            sess = await store.get_session(session_id)
            if sess is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "error": "session not found",
                        "code": "session_not_found",
                    },
                )
            owner = sess.get("owner") if hasattr(sess, "get") else None
            if owner != label:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error": "forbidden",
                        "code": "forbidden_audit_view",
                        "required_role": "admin",
                        "current_role": role,
                        "session_owner": owner,
                    },
                )
        else:
            # No session_id: force ``actor=<self label>`` so the
            # caller only sees their own actions. An explicit
            # ``actor=<other>`` is rejected (403) rather than
            # silently rewritten — the explicit-deny avoids the
            # confusing "I asked for bob's audit but got mine"
            # response shape.
            if actor is not None and actor != label:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error": "cannot filter by other actor",
                        "code": "forbidden_audit_filter",
                        "required_role": "admin",
                        "current_role": role,
                    },
                )
            actor = label

    try:
        rows, next_cursor = await store.list_audit(
            session_id=session_id,
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            after=after,
            limit=limit,
        )
    except (CursorInvalidError, CursorFilterMismatchError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": str(exc),
                "code": "cursor_invalid",
            },
        ) from exc

    items: list[dict[str, Any]] = []
    for r in rows:
        ts_val = r["ts"]
        ts_repr = (
            ts_val.isoformat() if hasattr(ts_val, "isoformat") else str(ts_val)
        )
        items.append(
            {
                "id": int(r["id"]),
                "ts": ts_repr,
                "actor": r["actor"],
                "action": r["action"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "metadata": r["metadata_json"] or {},
                "request_id": r["request_id"],
            }
        )
    return {
        "items": items,
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
    }
