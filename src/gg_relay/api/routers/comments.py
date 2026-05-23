"""Session comments endpoints — Plan 8 D8.5 / Task 7.

Surface:

  * ``POST   /api/v1/sessions/{session_id}/comments`` (submitter+) —
    create a new comment.
  * ``GET    /api/v1/sessions/{session_id}/comments``               —
    list comments for one session, oldest first; soft-deleted rows
    excluded.
  * ``PATCH  /api/v1/comments/{comment_id}`` (author only)          —
    edit body markdown; ``updated_at`` bumps.
  * ``DELETE /api/v1/comments/{comment_id}`` (author or admin)       —
    soft delete via ``deleted_at`` tombstone.

Authorization rules (matches Plan 8 §7 Task 7 spec):

  * Create: ``submitter+`` (the route dependency raises 403 with
    ``insufficient_role`` for viewers).
  * Edit: only the original ``author`` (server-side label, NOT a
    user-controlled field) may PATCH. Admins are intentionally not
    granted edit rights — moderation goes through delete instead so
    the original intent stays preserved.
  * Delete: ``author`` OR ``admin``. Admin override is enforced
    by checking ``ROLE_HIERARCHY[role] >= ROLE_HIERARCHY["admin"]``
    using the same hierarchy as :func:`require_role` so a future
    role bump propagates uniformly.

Audit (Task 5 / D8.4):
  * Every mutation calls
    :meth:`gg_relay.api.audit_service.AuditService.record` with
    ``target_type='comment'`` and ``target_id=str(comment.id)``. The
    metadata bundle carries ``{session_id, body_len}`` for create /
    update and ``{session_id}`` for delete.
  * Audit writes happen AFTER the business mutation here (not in the
    same tx via ``conn=``) because the comment write is a single
    INSERT / UPDATE that has already committed by the time the audit
    line lands; the durable-outbox pattern is reserved for the multi-
    step mutations (session lifecycle) where partial commits would
    leave the audit log inconsistent. For comments, a missed audit
    on a crash mid-flight is recovered by the fallback middleware
    writing an ``unknown_mutation`` row.

Response shapes are flat ``dict[str, Any]`` so the dashboard can
consume them without a generated pydantic client; ISO-8601 strings
are emitted for timestamps so a future GET render passes the values
straight to ``new Date(...)``.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from gg_relay.api.dependencies.require_role import (
    ROLE_HIERARCHY,
    _resolve_role,
    require_role,
)
from gg_relay.comments.sanitizer import render_safe

logger = logging.getLogger("gg_relay.api.routers.comments")

router = APIRouter(tags=["comments"])

# Body length cap. Mirrors the dashboard textarea ``maxlength`` so the
# UI and the API agree on the limit. Generous enough for paragraph
# discussions, tight enough to bound the bleach render budget and the
# audit metadata size.
_BODY_MIN_LEN = 1
_BODY_MAX_LEN = 8000


class CommentCreate(BaseModel):
    """POST body for create. ``body`` is raw markdown the server
    sanitises before storing."""

    body: str = Field(min_length=_BODY_MIN_LEN, max_length=_BODY_MAX_LEN)


class CommentUpdate(BaseModel):
    """PATCH body for edit. Same shape + bounds as create."""

    body: str = Field(min_length=_BODY_MIN_LEN, max_length=_BODY_MAX_LEN)


def _iso(value: Any) -> Any:
    """Best-effort ISO-8601 serialisation for datetime-ish values.

    SQLite returns naive datetimes for the timestamp columns; the
    repository's freshly-inserted rows are tz-aware. ``isoformat()``
    works on both. Non-datetime values pass through unchanged.
    """
    return value.isoformat() if hasattr(value, "isoformat") else value


def _serialize(row: Any) -> dict[str, Any]:
    """Project a row dict / RowMapping into the wire response shape.

    Soft-delete tombstone is intentionally absent from the response —
    the list query already filters live rows, and exposing
    ``deleted_at`` would tempt clients to special-case it instead of
    treating the absence of a comment as "gone".
    """
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "author": row["author"],
        "body_markdown": row["body_markdown"],
        "body_html": row["body_html"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


@router.post(
    "/sessions/{session_id}/comments",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("submitter"))],
)
async def create_comment(
    request: Request,
    session_id: str,
    payload: CommentCreate,
) -> dict[str, Any]:
    """Create a comment on ``session_id``.

    Auth: ``submitter+`` via the route ``Depends``. The session must
    exist (404 otherwise). ``author`` is captured server-side from
    ``request.state.api_key_label`` so a malicious client cannot
    impersonate another user by spoofing a JSON field.

    The body is sanitised through :func:`render_safe` BEFORE the
    store write so the database always holds the post-bleach HTML.
    Audit row lands after the insert with
    ``action='comment_create'``.
    """
    store = request.app.state.store
    audit = request.app.state.audit_service
    label = getattr(request.state, "api_key_label", None) or "anon"

    sess = await store.get_session(session_id)
    if sess is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "session not found",
                "code": "session_not_found",
            },
        )

    body_html = render_safe(payload.body)
    row = await store.create_comment(
        session_id=session_id,
        author=label,
        body_markdown=payload.body,
        body_html=body_html,
    )
    await audit.record(
        actor=label,
        action="comment_create",
        target_type="comment",
        target_id=str(row["id"]),
        metadata={"session_id": session_id, "body_len": len(payload.body)},
        request_id=getattr(request.state, "request_id", None),
    )
    return _serialize(row)


@router.get("/sessions/{session_id}/comments")
async def list_comments(
    request: Request,
    session_id: str,
    limit: int = 100,
) -> dict[str, Any]:
    """List comments for one session, oldest first.

    Soft-deleted rows are excluded; ``limit`` caps the response
    (default 100 — per-session threads are bounded by UX). 404 if
    the session does not exist so a typo'd id surfaces clearly
    instead of returning an empty list that the dashboard would
    confuse with "no discussion yet".
    """
    store = request.app.state.store
    sess = await store.get_session(session_id)
    if sess is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "session not found",
                "code": "session_not_found",
            },
        )
    rows = await store.list_comments(session_id=session_id, limit=limit)
    return {"items": [_serialize(r) for r in rows]}


@router.patch("/comments/{comment_id}")
async def update_comment(
    request: Request,
    comment_id: int,
    payload: CommentUpdate,
) -> dict[str, Any]:
    """Edit a comment body.

    Authorization: only the original ``author`` (server-side label)
    may edit. Admins are deliberately excluded — moderation flows
    through DELETE so the original wording is preserved.

    404 when the comment is missing or already soft-deleted; 403
    with ``forbidden_comment_edit`` when the caller is not the
    author. The 409 ``comment_update_failed`` branch covers the
    rare concurrent-delete race where the row exists at read time
    but a parallel DELETE soft-deletes it before the UPDATE lands.
    """
    store = request.app.state.store
    audit = request.app.state.audit_service
    label = getattr(request.state, "api_key_label", None) or "anon"

    existing = await store.get_comment(comment_id=comment_id)
    if existing is None or existing["deleted_at"] is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "comment not found",
                "code": "comment_not_found",
            },
        )
    if existing["author"] != label:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "only author may edit",
                "code": "forbidden_comment_edit",
                "comment_author": existing["author"],
                "current_actor": label,
            },
        )

    body_html = render_safe(payload.body)
    ok = await store.update_comment(
        comment_id=comment_id,
        body_markdown=payload.body,
        body_html=body_html,
    )
    if not ok:
        # Existed at read time, missing at write time → a concurrent
        # delete tombstoned the row. Surface as 409 so the dashboard
        # can refresh and re-render the deleted state.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "comment update failed (concurrent delete?)",
                "code": "comment_update_failed",
            },
        )
    await audit.record(
        actor=label,
        action="comment_update",
        target_type="comment",
        target_id=str(comment_id),
        metadata={
            "session_id": existing["session_id"],
            "body_len": len(payload.body),
        },
        request_id=getattr(request.state, "request_id", None),
    )
    updated = await store.get_comment(comment_id=comment_id)
    # ``updated`` is guaranteed non-null because the update succeeded;
    # the ``assert`` keeps mypy happy without an Optional access path.
    assert updated is not None
    return _serialize(updated)


@router.delete(
    "/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_comment(request: Request, comment_id: int) -> None:
    """Soft-delete a comment.

    Authorization: ``author`` OR ``admin`` (per Plan 8 §7 Task 7).
    The admin check reuses :data:`ROLE_HIERARCHY` so the comparison
    matches every other ``require_role`` site — a future
    "moderator" tier between submitter and admin would propagate
    here without code changes if added below ``admin`` in the
    hierarchy.

    Idempotent at the 404 boundary: a second DELETE on the same
    comment returns the same 404 ``comment_not_found`` body because
    the soft-delete predicate filters out tombstoned rows.
    """
    store = request.app.state.store
    audit = request.app.state.audit_service
    label = getattr(request.state, "api_key_label", None) or "anon"
    role = _resolve_role(request)

    existing = await store.get_comment(comment_id=comment_id)
    if existing is None or existing["deleted_at"] is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "comment not found",
                "code": "comment_not_found",
            },
        )
    is_author = existing["author"] == label
    is_admin = ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY["admin"]
    if not (is_author or is_admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "only author or admin may delete",
                "code": "forbidden_comment_delete",
                "comment_author": existing["author"],
                "current_actor": label,
                "current_role": role,
            },
        )

    ok = await store.soft_delete_comment(comment_id=comment_id)
    if not ok:
        # Lost the race to a concurrent delete — converge on 404 so
        # the dashboard refresh hides the row uniformly regardless
        # of who clicked first.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "comment not found",
                "code": "comment_not_found",
            },
        )
    await audit.record(
        actor=label,
        action="comment_delete",
        target_type="comment",
        target_id=str(comment_id),
        metadata={"session_id": existing["session_id"]},
        request_id=getattr(request.state, "request_id", None),
    )
    return None


__all__ = ["router"]
