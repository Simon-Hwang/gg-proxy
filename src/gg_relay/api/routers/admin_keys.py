"""Admin API key self-service — Plan 8 Task 22 / D8.29.

Three endpoints, all admin-only (``require_role("admin")``):

  * ``POST /api/v1/admin/keys``               — mint a new key. Returns
    the plaintext exactly ONCE in the response body. The DB only
    ever stores ``hash_key(plaintext)``.
  * ``GET  /api/v1/admin/keys``               — list keys (never
    plaintext). Optional ``include_revoked`` query param surfaces
    soft-deleted rows for the audit view.
  * ``DELETE /api/v1/admin/keys/{label}``     — soft-delete one key.
    Refuses self-revoke (400 ``self_revoke_forbidden``) and refuses
    when the revoke would drop active admins to zero (400
    ``last_admin_revoke_forbidden``).

Cache invalidation is in-process only (single-worker tier — Plan 8
KeyInvalidateSubscriber multi-worker pub/sub is deferred). The admin
endpoints call ``resolver.invalidate_cache(...)`` inline so the very
next request reads the fresh state.

Every mutation writes an audit row through
:class:`gg_relay.api.audit_service.AuditService`. The fallback
middleware would catch missed inline writes but the explicit call is
preferred so the audit row carries the rich metadata
(``role``/``victim_role``/``expires_at``) that the fallback can't
reconstruct from the URL alone.
"""
from __future__ import annotations

import logging
import secrets as stdlib_secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from gg_relay.api.dependencies.require_role import require_role
from gg_relay.core.exceptions import ApiKeyConflictError

logger = logging.getLogger("gg_relay.api.admin_keys")

router = APIRouter(prefix="/admin/keys", tags=["admin-keys"])


class ApiKeyCreate(BaseModel):
    """POST body schema for ``POST /api/v1/admin/keys``.

    Field constraints:

      * ``label``           — printable label safe for log lines.
        Pattern ``[A-Za-z0-9_.-]+`` is the same charset the env
        parser allows for ``RELAY_API_KEYS_RAW`` (D7.26), so a
        label minted here can later be referenced in env-driven
        config without escaping.
      * ``role``            — restricted to the three legal roles
        so a typo doesn't silently create a key the
        ``require_role`` ladder can't compare against.
      * ``notes``           — free-form, capped at 500 chars to
        match the column.
      * ``expires_in_days`` — optional; when set the lifespan of
        the key is bounded by ``now + N days`` and the resolver
        refuses the key past that point.
    """

    label: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$"
    )
    role: str = Field(pattern=r"^(viewer|submitter|admin)$")
    notes: str | None = Field(default=None, max_length=500)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("admin"))],
)
async def create_api_key(
    request: Request, payload: ApiKeyCreate
) -> dict[str, Any]:
    """Mint a new api_key.

    The plaintext is returned ONCE in the response body and never
    stored. Operators MUST capture it from this response — there is
    no recovery path. The DB only ever sees ``hash_key(plaintext)``.

    409 ``api_key_label_conflict`` on duplicate label. Cache is
    invalidated by label so a label that was previously revoked +
    recreated doesn't serve a stale negative cache hit.
    """
    key_store = request.app.state.api_key_store
    audit = request.app.state.audit_service
    resolver = request.app.state.key_resolver
    caller_label = getattr(request.state, "api_key_label", "anon")

    raw_key = "rk_" + stdlib_secrets.token_urlsafe(32)
    expires_at: datetime | None = None
    if payload.expires_in_days is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(
            days=payload.expires_in_days
        )

    try:
        row = await key_store.create(
            label=payload.label,
            raw_key=raw_key,
            role=payload.role,
            created_by_label=caller_label,
            expires_at=expires_at,
            notes=payload.notes,
        )
    except ApiKeyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": str(exc),
                "code": "api_key_label_conflict",
            },
        ) from exc

    await audit.record(
        actor=caller_label,
        action="key_create",
        target_type="api_key",
        target_id=payload.label,
        metadata={
            "role": payload.role,
            "expires_at": (
                expires_at.isoformat() if expires_at is not None else None
            ),
        },
    )

    # Invalidate by label so a recreate-after-revoke wipes the prior
    # negative cache hit. The new positive entry will populate on
    # the next request.
    await resolver.invalidate_cache(label=payload.label)

    return {
        "label": row["label"],
        "role": row["role"],
        "created_at": row["created_at"].isoformat(),
        "expires_at": (
            expires_at.isoformat() if expires_at is not None else None
        ),
        "raw_key": raw_key,
        "notes": row["notes"],
        "warning": (
            "Save this raw_key NOW — it cannot be retrieved later."
        ),
    }


@router.get(
    "",
    dependencies=[Depends(require_role("admin"))],
)
async def list_api_keys(
    request: Request,
    include_revoked: Annotated[
        bool, Query(description="Surface soft-deleted rows.")
    ] = False,
) -> dict[str, Any]:
    """List api_keys, newest-first. NEVER includes plaintext.

    ``include_revoked=True`` surfaces soft-deleted rows so operators
    can audit prior keys; the default omits them so the working
    list stays compact.
    """
    key_store = request.app.state.api_key_store
    rows = await key_store.list(include_revoked=include_revoked)
    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "label": r["label"],
                "role": r["role"],
                "created_at": _iso_or_none(r["created_at"]),
                "created_by_label": r["created_by_label"],
                "expires_at": _iso_or_none(r["expires_at"]),
                "revoked_at": _iso_or_none(r["revoked_at"]),
                "last_used_at": _iso_or_none(r["last_used_at"]),
                "notes": r["notes"],
                "is_active": r["revoked_at"] is None,
            }
        )
    return {"items": items}


@router.delete(
    "/{label}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role("admin"))],
)
async def revoke_api_key(request: Request, label: str) -> None:
    """Soft-revoke one api_key by label.

    Refuses on three paths:

      * 404 ``api_key_not_found`` — the label doesn't exist or has
        already been revoked.
      * 400 ``self_revoke_forbidden`` — the caller is trying to
        revoke their own currently-active key. The defensive
        recovery flow is "mint another admin → switch keys → then
        revoke yourself" — surfaced as a clear error so a slip
        click doesn't lock the operator out.
      * 400 ``last_admin_revoke_forbidden`` — the target is an
        admin and revoking it would drop the active admin count
        to zero.
    """
    key_store = request.app.state.api_key_store
    audit = request.app.state.audit_service
    resolver = request.app.state.key_resolver
    caller_label = getattr(request.state, "api_key_label", "anon")

    target = await key_store.get_by_label(label)
    if target is None or target["revoked_at"] is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "api_key not found or already revoked",
                "code": "api_key_not_found",
            },
        )

    if label == caller_label:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": (
                    "Cannot revoke your own active key. "
                    "Create another admin key first, switch, then revoke."
                ),
                "code": "self_revoke_forbidden",
            },
        )

    if target["role"] == "admin":
        active_admins = await key_store.count_active_admins()
        if active_admins <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "At least one admin key must remain active.",
                    "code": "last_admin_revoke_forbidden",
                },
            )

    await key_store.revoke(label=label)
    await audit.record(
        actor=caller_label,
        action="key_revoke",
        target_type="api_key",
        target_id=label,
        metadata={"victim_role": target["role"]},
    )
    await resolver.invalidate_cache(
        label=label, key_hash=target["key_hash"]
    )
    return None


def _iso_or_none(value: Any) -> str | None:
    """Render a datetime-ish value as ISO-8601 or pass strings through.

    SQLAlchemy returns datetimes on Postgres but bare strings on
    SQLite when the column wasn't bound to a Python datetime — the
    helper smooths over the difference so the JSON response shape
    is stable across both dialects.
    """
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
