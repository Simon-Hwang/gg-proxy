"""Prompt template CRUD endpoints — Plan 8 D8.24 / Task 14.

Surface:

  * ``POST   /api/v1/templates`` (submitter+)               — create.
  * ``GET    /api/v1/templates`` (any authenticated caller) — list
    templates visible to the caller. ``include_others=True`` admin-
    only flag also lists other users' private templates (moderation
    surface).
  * ``GET    /api/v1/templates/{template_id}``              — fetch
    one template. Visibility rules mirror the list endpoint: 403
    ``forbidden_template_view`` for a private template owned by
    another user (unless the caller is admin).
  * ``PATCH  /api/v1/templates/{template_id}`` (author or admin)
                                                            — update
    body / description / shared / tags.
  * ``DELETE /api/v1/templates/{template_id}`` (author or admin)
                                                            — hard
    delete. Audit row preserves the moderation trail.

Visibility model:

  * Creator always sees and may edit their own templates.
  * ``shared=True`` templates are visible to every submitter+ in
    list / get.
  * ``shared=False`` private templates are visible only to the
    creator; admins may opt into seeing other users' private
    templates via the ``include_others`` query parameter.
  * Edit / delete are creator-or-admin regardless of the ``shared``
    flag (a shared template is still authored by one user; other
    users may USE it but not mutate it).

Audit (Plan 8 D8.4 / Task 5):

  * Every mutation writes an ``audit_log`` row through
    :meth:`AuditService.record` with ``target_type='template'`` +
    ``target_id=str(template.id)`` and the action in
    ``{template_create, template_update, template_delete}``.
  * Audit writes happen AFTER the business mutation (not in the
    same tx via ``conn=``) because the template write is a single
    INSERT / UPDATE / DELETE that has already committed by the
    time the audit line lands — matching the comments router's
    convention.

Conflict handling:

  * ``POST`` collisions on the ``(creator, name)`` unique key
    surface as :class:`TemplateConflictError` from the store, which
    we map to 409 with ``code='template_name_conflict'`` so the
    dashboard can render "rename and retry" without parsing free-
    form error strings.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from gg_relay.api.dependencies.require_role import (
    ROLE_HIERARCHY,
    _resolve_role,
    require_role,
)
from gg_relay.core.exceptions import TemplateConflictError

logger = logging.getLogger("gg_relay.api.routers.templates")

router = APIRouter(prefix="/templates", tags=["templates"])

# Body length caps. Generous enough for paragraph-sized prompts while
# bounding the audit metadata budget.
_NAME_MAX = 128
_PROMPT_MAX = 20_000
_DESC_MAX = 500
_TAGS_MAX = 500


class TemplateCreate(BaseModel):
    """POST body for create."""

    name: str = Field(min_length=1, max_length=_NAME_MAX)
    prompt: str = Field(min_length=1, max_length=_PROMPT_MAX)
    description: str | None = Field(default=None, max_length=_DESC_MAX)
    shared: bool = False
    tags: str | None = Field(default=None, max_length=_TAGS_MAX)


class TemplateUpdate(BaseModel):
    """PATCH body for update — every field optional so a partial
    edit only touches the supplied keys."""

    prompt: str | None = Field(default=None, min_length=1, max_length=_PROMPT_MAX)
    description: str | None = Field(default=None, max_length=_DESC_MAX)
    shared: bool | None = None
    tags: str | None = Field(default=None, max_length=_TAGS_MAX)


def _iso(value: Any) -> Any:
    """Best-effort ISO-8601 serialisation for datetime-ish values.

    SQLite returns naive datetimes for the timestamp columns; the
    repository's freshly-inserted rows are tz-aware. ``isoformat()``
    works on both. Non-datetime values pass through unchanged.
    """
    return value.isoformat() if hasattr(value, "isoformat") else value


def _serialize(row: Any) -> dict[str, Any]:
    """Project a row dict / RowMapping into the wire response shape.

    ``shared`` is normalised through ``bool(...)`` so SQLite (stores
    0/1 ints for ``Boolean``) and Postgres (stores true/false) emit
    the same JSON shape.
    """
    return {
        "id": row["id"],
        "name": row["name"],
        "creator": row["creator"],
        "prompt": row["prompt"],
        "description": row["description"],
        "shared": bool(row["shared"]),
        "tags": row["tags"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _is_admin(request: Request) -> bool:
    """Resolve whether the caller satisfies the admin tier.

    Uses the same :data:`ROLE_HIERARCHY` comparison as the comments
    router so a future "moderator" tier between submitter and admin
    propagates uniformly without per-router duplication.
    """
    role = _resolve_role(request)
    return ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY["admin"]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("submitter"))],
)
async def create_template(
    request: Request, payload: TemplateCreate
) -> dict[str, Any]:
    """Create a prompt template (Plan 8 D8.24 / Task 14).

    Auth: ``submitter+`` via the route ``Depends``. The creator is
    captured server-side from ``request.state.api_key_label`` so a
    malicious client cannot impersonate another user by spoofing a
    JSON field.

    409 ``template_name_conflict`` when the ``(creator, name)``
    pair is already taken (router maps
    :class:`TemplateConflictError` raised by the store).
    """
    store = request.app.state.store
    audit = getattr(request.app.state, "audit_service", None)
    label = getattr(request.state, "api_key_label", None) or "anon"

    try:
        row = await store.create_template(
            name=payload.name,
            creator=label,
            prompt=payload.prompt,
            description=payload.description,
            shared=payload.shared,
            tags=payload.tags,
        )
    except TemplateConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": str(exc),
                "code": "template_name_conflict",
            },
        ) from exc

    if audit is not None:
        try:
            await audit.record(
                actor=label,
                action="template_create",
                target_type="template",
                target_id=str(row["id"]),
                metadata={
                    "name": payload.name,
                    "shared": bool(payload.shared),
                },
                request_id=getattr(request.state, "request_id", None),
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("audit record_audit failed for template_create")
    return _serialize(row)


@router.get("")
async def list_templates(
    request: Request,
    include_others: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """List templates visible to the caller.

    Non-admin callers see only their own templates plus every
    ``shared=True`` row. Admins may flip ``include_others=true`` to
    additionally see other users' private templates (moderation
    surface); without the flag the admin's listing matches the
    non-admin view to avoid accidental privacy leaks in the default
    case.
    """
    store = request.app.state.store
    label = getattr(request.state, "api_key_label", None) or "anon"
    is_admin = _is_admin(request)
    rows = await store.list_templates(
        actor=label,
        is_admin=is_admin,
        include_others=include_others,
        limit=limit,
    )
    return {"items": [_serialize(r) for r in rows]}


@router.get("/{template_id}")
async def get_template(
    request: Request, template_id: int
) -> dict[str, Any]:
    """Fetch a single template.

    Visibility mirrors :meth:`list_templates` per-row:
      * 404 ``template_not_found`` when the id doesn't exist.
      * 403 ``forbidden_template_view`` when the row is private and
        the caller is neither the creator nor an admin.
    """
    store = request.app.state.store
    label = getattr(request.state, "api_key_label", None) or "anon"
    is_admin = _is_admin(request)

    row = await store.get_template(template_id=template_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "template not found",
                "code": "template_not_found",
            },
        )
    if not bool(row["shared"]) and row["creator"] != label and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "private template not visible",
                "code": "forbidden_template_view",
            },
        )
    return _serialize(row)


@router.patch("/{template_id}")
async def update_template(
    request: Request,
    template_id: int,
    payload: TemplateUpdate,
) -> dict[str, Any]:
    """Edit a template's body / visibility / tags.

    Authorization: only the original ``creator`` (server-side label)
    or an ``admin`` may PATCH. Other users see the template (if it's
    shared) but cannot mutate it.

    404 when the template is missing; 403 ``forbidden_template_edit``
    when the caller is neither the creator nor an admin.
    """
    store = request.app.state.store
    audit = getattr(request.app.state, "audit_service", None)
    label = getattr(request.state, "api_key_label", None) or "anon"
    is_admin = _is_admin(request)

    existing = await store.get_template(template_id=template_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "template not found",
                "code": "template_not_found",
            },
        )
    if existing["creator"] != label and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "only creator or admin may edit",
                "code": "forbidden_template_edit",
                "template_creator": existing["creator"],
                "current_actor": label,
            },
        )

    ok = await store.update_template(
        template_id=template_id,
        prompt=payload.prompt,
        description=payload.description,
        shared=payload.shared,
        tags=payload.tags,
    )
    if not ok:
        # Existed at read time, missing at write time → a concurrent
        # delete removed the row. Surface as 404 so the dashboard
        # refresh hides the row uniformly.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "template not found",
                "code": "template_not_found",
            },
        )

    if audit is not None:
        try:
            await audit.record(
                actor=label,
                action="template_update",
                target_type="template",
                target_id=str(template_id),
                metadata={"name": existing["name"]},
                request_id=getattr(request.state, "request_id", None),
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("audit record_audit failed for template_update")

    updated = await store.get_template(template_id=template_id)
    # ``updated`` is guaranteed non-null because the update succeeded;
    # the ``assert`` keeps mypy happy without an Optional access path.
    assert updated is not None
    return _serialize(updated)


@router.delete(
    "/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_template(request: Request, template_id: int) -> None:
    """Hard-delete a template.

    Authorization: ``creator`` OR ``admin``. Other users (even ones
    using the shared template) cannot delete it. 404 idempotency:
    a second DELETE on the same id returns the same body so
    concurrent deletes converge on a uniform response.
    """
    store = request.app.state.store
    audit = getattr(request.app.state, "audit_service", None)
    label = getattr(request.state, "api_key_label", None) or "anon"
    is_admin = _is_admin(request)

    existing = await store.get_template(template_id=template_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "template not found",
                "code": "template_not_found",
            },
        )
    if existing["creator"] != label and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "only creator or admin may delete",
                "code": "forbidden_template_delete",
                "template_creator": existing["creator"],
                "current_actor": label,
            },
        )

    removed = await store.delete_template(template_id=template_id)
    if not removed:
        # Lost the race to a concurrent delete — converge on 404.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "template not found",
                "code": "template_not_found",
            },
        )

    if audit is not None:
        try:
            await audit.record(
                actor=label,
                action="template_delete",
                target_type="template",
                target_id=str(template_id),
                metadata={"name": existing["name"]},
                request_id=getattr(request.state, "request_id", None),
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("audit record_audit failed for template_delete")
    return None


__all__ = ["router"]
