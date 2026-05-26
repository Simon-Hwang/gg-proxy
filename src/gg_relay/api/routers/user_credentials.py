"""Per-user upstream credentials API — Plan v3 §B.4 + §B.5.

Two route groups, both backed by the same
:class:`gg_relay.store.user_credentials.UserCredentialsStore`:

  * ``/api/v1/me/credentials/...``    — self-service. Any
    authenticated submitter+ may read/write/delete *their own* rows
    (keyed off ``request.state.api_key_label``). Viewer is blocked
    because a viewer cannot submit a session and therefore has no
    reason to store an upstream credential.
  * ``/api/v1/admin/credentials/...`` — operator-only. Admin may
    read/write/delete rows on behalf of any user. Same allowlist
    enforcement as ``/me/`` (admin is NOT trusted to set
    ``PATH``/``LD_PRELOAD``/``PYTHONPATH`` — defense in depth).

Plan v3 §B.5 — the env-name allowlist is the most important security
boundary in this router. If a caller could store ``LD_PRELOAD`` as a
"credential", they could load a malicious shared library into every
spawned ``claude`` subprocess on the host. The allowlist is enforced
by the shared :func:`_validate_env_name` helper invoked from EVERY
write path (``/me/`` PUT, ``/admin/`` PUT, ``/me/`` DELETE,
``/admin/`` DELETE).

Plaintext discipline:

  * NEVER returned by any route — list / get / put / delete all
    project to metadata-only views (``env_name``,
    ``created_by_label``, ``updated_at``, etc.).
  * NEVER logged. The router does not log the value at all; the
    audit row only carries ``env_name`` + length-class.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from gg_relay.api.dependencies.require_role import require_role
from gg_relay.store.user_credentials import (
    UserCredentialsFeatureDisabled,
    UserCredentialsStore,
)

logger = logging.getLogger("gg_relay.api.user_credentials")


# Plan v3 §B.5 — hard-coded allowlist. Adding to this list requires
# a code review; runtime env-var injection is intentionally NOT
# supported. The four supported providers cover Anthropic direct,
# AWS Bedrock, Google Vertex, and self-hosted (BASE_URL).
ALLOWED_ENV_NAMES: frozenset[str] = frozenset({
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLOUD_ML_REGION",
    "ANTHROPIC_VERTEX_PROJECT_ID",
})


def _validate_env_name(env_name: str) -> None:
    """Reject any ``env_name`` not in :data:`ALLOWED_ENV_NAMES`.

    Plan v3 §B.5 — shared by both ``/me/`` and ``/admin/`` write paths.
    Admin is NOT trusted to bypass this list; the v2-Santa reviewer
    flagged the missing admin-side enforcement as a 4-line bug that
    would have let an admin store ``LD_PRELOAD`` and silently
    backdoor every session on the host. Closes that exact gap.

    Raises 400 ``env_name_not_allowed`` with the offending name and
    the full allowed list in the response body so the caller can
    self-correct without reading the source.
    """
    if env_name not in ALLOWED_ENV_NAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": (
                    f"env_name {env_name!r} is not in the allowed list; "
                    f"runtime env-var injection is rejected to prevent "
                    f"PATH/LD_PRELOAD smuggling attacks"
                ),
                "code": "env_name_not_allowed",
                "allowed": sorted(ALLOWED_ENV_NAMES),
            },
        )


def _get_store(request: Request) -> UserCredentialsStore:
    """Lazy access to ``app.state.user_credentials_store`` + feature
    gating. Returns 503 if the feature is disabled so the client
    distinguishes config-level rejection from auth-level rejection."""
    store: UserCredentialsStore | None = getattr(
        request.app.state, "user_credentials_store", None
    )
    if store is None or not store.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": (
                    "per-user upstream credentials feature is disabled; "
                    "set RELAY_CREDENTIALS_ENCRYPTION_KEY to enable"
                ),
                "code": "user_credentials_disabled",
            },
        )
    return store


def _project_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """Strip any column that could ever leak plaintext + format ts as iso.

    Used by every read path so the JSON response is consistent across
    list / put / admin views. Plan v3 §B.4 — defence in depth: even
    though ``list_for_user`` already drops ``value_encrypted``, this
    helper enforces the contract at the router boundary too.
    """
    return {
        "id": row.get("id"),
        "user_label": row.get("user_label"),
        "env_name": row.get("env_name"),
        "key_fingerprint": row.get("key_fingerprint"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "created_by_label": row.get("created_by_label"),
        "notes": row.get("notes"),
    }


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# Plan v3 §B.7 follow-up — fixed-length mask used by both the user
# self-service page (`/dashboard/me/credentials`) and the admin
# pre-reveal table cell (`/dashboard/admin/credentials`). 8 bullets +
# last 4 chars deliberately:
#   * Last-N gives the user enough context to tell two of their own
#     keys apart without leaking either.
#   * Fixed bullet count hides the original length (length itself is a
#     side-channel that helps an attacker fingerprint the provider).
#   * Values shorter than the visible-tail are fully masked — never
#     return a non-trivial prefix of a tiny secret.
_MASK_VISIBLE_TAIL: int = 4
_MASK_PREFIX_DOTS: int = 8


def mask_credential_value(value: str | None) -> str:
    """Return a fixed-shape masked rendering of ``value``.

    Empty / None → empty string. Anything ≤ visible-tail → all bullets
    (no plaintext leak even of "short" values). Anything longer →
    fixed-length bullet prefix + last 4 chars.
    """
    if not value:
        return ""
    if len(value) <= _MASK_VISIBLE_TAIL:
        return "\u2022" * len(value)
    return ("\u2022" * _MASK_PREFIX_DOTS) + value[-_MASK_VISIBLE_TAIL:]


def length_class(value: str) -> str:
    """Public alias for :func:`_length_class` so the dashboard reveal
    route can record the same audit metadata shape that the API
    upsert / delete paths already emit."""
    return _length_class(value)


# ── pydantic schemas ────────────────────────────────────────────────────


class CredentialUpsert(BaseModel):
    """PUT body for ``/me/credentials/{env_name}`` and
    ``/admin/credentials/{user_label}/{env_name}``.

    ``value`` is the plaintext upstream credential. It is encrypted
    at the store boundary and never logged.
    """

    value: str = Field(min_length=1, max_length=4096)
    notes: str | None = Field(default=None, max_length=512)


# ── routers ─────────────────────────────────────────────────────────────


me_router = APIRouter(prefix="/me/credentials", tags=["me-credentials"])
admin_router = APIRouter(
    prefix="/admin/credentials", tags=["admin-credentials"]
)


# ── /me/credentials ────────────────────────────────────────────────────


@me_router.get(
    "",
    dependencies=[Depends(require_role("submitter"))],
)
async def list_my_credentials(request: Request) -> dict[str, Any]:
    """List the calling user's stored credentials (metadata only).

    Keys off ``request.state.api_key_label``; viewer is rejected by
    :func:`require_role` (storing creds you can't use is meaningless).
    """
    store = _get_store(request)
    label = getattr(request.state, "api_key_label", None) or "anon"
    rows = await store.list_for_user(label)
    return {
        "user_label": label,
        "credentials": [_project_metadata(r) for r in rows],
    }


@me_router.put(
    "/{env_name}",
    dependencies=[Depends(require_role("submitter"))],
)
async def upsert_my_credential(
    request: Request, env_name: str, payload: CredentialUpsert
) -> dict[str, Any]:
    """Create or overwrite the calling user's credential row.

    ``env_name`` is validated against :data:`ALLOWED_ENV_NAMES`
    BEFORE the value reaches the store (Plan v3 §B.5). The response
    is metadata-only — the supplied value is never echoed back.
    """
    _validate_env_name(env_name)
    store = _get_store(request)
    label = getattr(request.state, "api_key_label", None) or "anon"
    audit = getattr(request.app.state, "audit_service", None)

    try:
        row = await store.upsert(
            user_label=label,
            env_name=env_name,
            value=payload.value,
            actor_label=label,
            notes=payload.notes,
        )
    except UserCredentialsFeatureDisabled as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": str(exc),
                "code": "user_credentials_disabled",
            },
        ) from exc

    if audit is not None:
        await audit.record(
            actor=label,
            action="user_credential_upsert",
            target_type="user_credential",
            target_id=f"{label}/{env_name}",
            metadata={
                "env_name": env_name,
                "value_length_class": _length_class(payload.value),
                "self_service": True,
            },
        )

    return _project_metadata(row)


@me_router.delete(
    "/{env_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role("submitter"))],
)
async def delete_my_credential(request: Request, env_name: str) -> None:
    """Idempotently delete the calling user's credential row."""
    _validate_env_name(env_name)
    store = _get_store(request)
    label = getattr(request.state, "api_key_label", None) or "anon"
    audit = getattr(request.app.state, "audit_service", None)

    removed = await store.delete(user_label=label, env_name=env_name)
    if audit is not None:
        await audit.record(
            actor=label,
            action="user_credential_delete",
            target_type="user_credential",
            target_id=f"{label}/{env_name}",
            metadata={
                "env_name": env_name,
                "row_removed": removed,
                "self_service": True,
            },
        )


# ── /admin/credentials ─────────────────────────────────────────────────


@admin_router.get(
    "",
    dependencies=[Depends(require_role("admin"))],
)
async def list_all_credentials(request: Request) -> dict[str, Any]:
    """Admin view: every row across every user (metadata only)."""
    store = _get_store(request)
    rows = await store.list_all()
    return {"credentials": [_project_metadata(r) for r in rows]}


@admin_router.get(
    "/bricked",
    dependencies=[Depends(require_role("admin"))],
)
async def list_bricked_credentials(request: Request) -> dict[str, Any]:
    """Admin view: rows whose fingerprint != current key fingerprint."""
    store = _get_store(request)
    rows = await store.list_bricked()
    return {
        "credentials": [
            {
                "id": r.get("id"),
                "user_label": r.get("user_label"),
                "env_name": r.get("env_name"),
                "key_fingerprint": r.get("key_fingerprint"),
                "updated_at": _iso(r.get("updated_at")),
            }
            for r in rows
        ]
    }


@admin_router.put(
    "/{user_label}/{env_name}",
    dependencies=[Depends(require_role("admin"))],
)
async def upsert_user_credential(
    request: Request,
    user_label: str,
    env_name: str,
    payload: CredentialUpsert,
) -> dict[str, Any]:
    """Admin override: create or overwrite ANY user's credential row.

    ``env_name`` allowlist enforcement is IDENTICAL to the ``/me/``
    path — admin is not trusted to set ``PATH`` / ``LD_PRELOAD``.
    The row's ``created_by_label`` reflects the admin's label so
    the user can see that an admin touched the row (audit
    transparency).
    """
    _validate_env_name(env_name)
    store = _get_store(request)
    admin_label = (
        getattr(request.state, "api_key_label", None) or "admin"
    )
    audit = getattr(request.app.state, "audit_service", None)

    try:
        row = await store.upsert(
            user_label=user_label,
            env_name=env_name,
            value=payload.value,
            actor_label=admin_label,
            notes=payload.notes,
        )
    except UserCredentialsFeatureDisabled as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": str(exc),
                "code": "user_credentials_disabled",
            },
        ) from exc

    if audit is not None:
        await audit.record(
            actor=admin_label,
            action="user_credential_admin_upsert",
            target_type="user_credential",
            target_id=f"{user_label}/{env_name}",
            metadata={
                "env_name": env_name,
                "value_length_class": _length_class(payload.value),
                "self_service": False,
                "victim_label": user_label,
            },
        )

    return _project_metadata(row)


@admin_router.delete(
    "/{user_label}/{env_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role("admin"))],
)
async def delete_user_credential(
    request: Request, user_label: str, env_name: str
) -> None:
    """Admin override: delete any user's credential row."""
    _validate_env_name(env_name)
    store = _get_store(request)
    admin_label = (
        getattr(request.state, "api_key_label", None) or "admin"
    )
    audit = getattr(request.app.state, "audit_service", None)

    removed = await store.delete(user_label=user_label, env_name=env_name)
    if audit is not None:
        await audit.record(
            actor=admin_label,
            action="user_credential_admin_delete",
            target_type="user_credential",
            target_id=f"{user_label}/{env_name}",
            metadata={
                "env_name": env_name,
                "row_removed": removed,
                "self_service": False,
                "victim_label": user_label,
            },
        )


def _length_class(value: str) -> str:
    """Bucket the value length so the audit log carries SOMETHING
    sortable about the credential without exposing the value itself.
    Buckets are chosen to distinguish common provider shapes (Anthropic
    sk-... ~108 chars, Vertex JSON keyfile thousands)."""
    n = len(value)
    if n <= 32:
        return "short"
    if n <= 128:
        return "medium"
    if n <= 1024:
        return "long"
    return "huge"
