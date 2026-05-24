"""Role-based access control dependency (Plan 8 D8.22).

This module provides FastAPI ``Depends(...)`` helpers — not Starlette
middleware. The plan brief calls it "require_role middleware" because
it acts like one mentally (drop-in authz at the route boundary), but
the implementation is a per-endpoint dependency so individual routes
can opt into different policies (admin-only / submitter-or-own /
viewer fall-through). A global middleware would have to learn every
route's policy, which is exactly the coupling we want to avoid.

Three-tier role hierarchy (low → high):

  * ``viewer``    — read-only on all endpoints.
  * ``submitter`` — create sessions, resolve own HITL, pause/resume
                    own session.
  * ``admin``     — cancel / retry / DELETE any session, and (Task 22)
                    administer roles via the dashboard.

Role resolution order (per request):

  1. Read ``request.state.api_key_label`` set by
     :class:`APIKeyAuthMiddleware`. Missing label means the request
     bypassed auth (un-authed test path, exempt webhook); we return
     ``"viewer"`` — the least-privileged role — as the safe default.
  2. Look up ``cfg.role_mapping[label]``. ``cfg.role_mapping`` is
     parsed from ``RELAY_ROLE_MAPPING_RAW`` (``alice=admin,bob=submitter``).
  3. A label that is not present in the map falls back to ``viewer``.

When ``cfg.role_mapping`` itself is empty, every label resolves to
``viewer``. The config's ``validate_required_secrets`` emits a
loud warning in production mode so operators notice that mutations
will silent-403 until they configure the map.

Usage::

    @router.post(
        "/api/v1/sessions",
        dependencies=[Depends(require_role("submitter"))],
    )
    async def create_session(...): ...

For own-session exceptions (cancel / DELETE / pause / resume one's
own session without the higher role):

    @router.post(
        "/api/v1/sessions/{session_id}/cancel",
        dependencies=[Depends(require_role_or_own_session("admin"))],
    )
    async def cancel_session(session_id: str, ...): ...

403 body shape (always a dict, never a bare string)::

    {
        "error": "forbidden",
        "code": "insufficient_role" | "not_owner" | "no_ownership_check_possible",
        "required_role": "<min_role>",
        "current_role": "<resolved role>",
        # optional, only on "not_owner":
        "session_owner": "<owner label>"
    }

The structured shape lets dashboards render an actionable message
(e.g. "ask admin to bump your role to submitter") without having
to parse free-form strings.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request, status

logger = logging.getLogger("gg_relay.api.dependencies.require_role")

# Lowest → highest. Tuple position == comparable integer level so
# ``ROLE_HIERARCHY[role]`` gives a direct >= comparable rank.
ROLE_HIERARCHY: dict[str, int] = {
    "viewer": 0,
    "submitter": 1,
    "admin": 2,
}


def _resolve_role(request: Request) -> str:
    """Resolve the caller's effective role for the current request.

    Resolution order:

      1. ``request.state.api_key_label`` MUST be present (set by
         :class:`APIKeyAuthMiddleware`); missing → ``viewer``.
      2. Plan 8 D8.29 + v2.3 BLOCKER 2 — when
         ``cfg.role_override_mode == 'db'`` (default) and the
         middleware populated ``request.state.api_key_role`` from the
         :class:`DBKeyResolver`, that role is the source of truth.
         The dashboard ``/admin/keys`` page can mutate roles at
         runtime via the DB column without touching env config.
      3. Otherwise (``role_override_mode == 'config'`` or no
         ``api_key_role`` on state — i.e. the Plan 7 frozen-dict
         middleware path that doesn't know about DB roles) we fall
         back to ``cfg.role_mapping[label]`` as the source.
      4. Any missing piece (no Config, no mapping entry) collapses
         to ``viewer`` — safe least-privileged default.

    Also caches the resolved role onto ``request.state.role`` so
    templates / audit logs downstream of the dependency can reuse
    the value without re-resolving.
    """
    label = getattr(request.state, "api_key_label", None)
    if label is None:
        return "viewer"

    app_state = getattr(request.app, "state", None)
    cfg = getattr(app_state, "config", None) if app_state is not None else None
    if cfg is None:
        return "viewer"

    override_mode = getattr(cfg, "role_override_mode", "db")
    if override_mode == "db":
        db_role = getattr(request.state, "api_key_role", None)
        if db_role:
            request.state.role = db_role
            return db_role

    role_map: dict[str, str] = getattr(cfg, "role_mapping", {}) or {}
    role = role_map.get(label, "viewer")
    # Cache for templates / audit consumers further down the stack.
    request.state.role = role
    return role


def require_role(min_role: str) -> Callable[[Request], Awaitable[str]]:
    """Return a dependency that 403s when the caller's role is too low.

    ``min_role`` must be one of :data:`ROLE_HIERARCHY` (``"viewer"``,
    ``"submitter"``, ``"admin"``). The dependency returns the
    caller's resolved role on success so endpoints can ``Depends``
    on it positionally if they want to use the value (most callers
    use ``dependencies=[Depends(require_role("submitter"))]`` and
    discard the return value).
    """
    if min_role not in ROLE_HIERARCHY:
        raise ValueError(
            f"unknown role {min_role!r}; "
            f"valid: {sorted(ROLE_HIERARCHY)!s}"
        )
    required_rank = ROLE_HIERARCHY[min_role]

    async def _check(request: Request) -> str:
        current_role = _resolve_role(request)
        if ROLE_HIERARCHY.get(current_role, 0) < required_rank:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "forbidden",
                    "code": "insufficient_role",
                    "required_role": min_role,
                    "current_role": current_role,
                },
            )
        return current_role

    return _check


def require_role_or_own_session(
    min_role: str,
) -> Callable[[Request, str], Awaitable[str]]:
    """Dependency: pass if role ≥ ``min_role`` *or* caller owns the session.

    Used for ``cancel`` / ``DELETE`` / ``pause`` / ``resume`` paths
    where a submitter can act on their own sessions even without the
    higher role. Ownership comparison is between
    ``request.state.api_key_label`` (set by the API key middleware,
    Plan 7 D7.26) and the ``owner`` column on the session row
    (auto-attributed from the same label at submit time).

    The path parameter MUST be called ``session_id`` to match the
    router signature — FastAPI resolves the dependency's
    ``session_id`` argument against the endpoint's path parameter
    of the same name. Renaming on the router would break the
    auto-binding.

    403 ``not_owner`` is preferred over a silent 404 when the
    session exists but belongs to someone else: the operator needs
    to know the action *would* work for the rightful owner. We
    still 404 cleanly when the session does not exist, so the
    "not found" / "forbidden" branches stay distinct.
    """
    role_check = require_role(min_role)

    async def _check(request: Request, session_id: str) -> str:
        # Fast path — the caller already has the minimum role; no
        # need to hit the store for ownership.
        try:
            return await role_check(request)
        except HTTPException:
            pass  # insufficient role, fall through to ownership check

        label = getattr(request.state, "api_key_label", None)
        app_state = getattr(request.app, "state", None)
        store = (
            getattr(app_state, "store", None)
            if app_state is not None
            else None
        )
        if store is None or label is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "forbidden",
                    "code": "no_ownership_check_possible",
                    "required_role": min_role,
                    "current_role": _resolve_role(request),
                },
            )

        try:
            session: Any = await store.get_session(session_id)
        except Exception:
            # Store errors during an authz check must not leak as a
            # 500: log it (so ops can diagnose) and refuse the
            # action conservatively.
            logger.exception(
                "ownership check failed for session_id=%s", session_id
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "forbidden",
                    "code": "ownership_check_error",
                    "required_role": min_role,
                    "current_role": _resolve_role(request),
                },
            ) from None

        if session is None:
            # Session genuinely doesn't exist — the eventual handler
            # body would 404 anyway, but surfacing it here avoids the
            # confusing "403 then 404" sequence on retry.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "not_found",
                    "code": "session_not_found",
                },
            )

        owner = session.get("owner") if hasattr(session, "get") else None
        if owner != label:
            current_role = _resolve_role(request)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "forbidden",
                    "code": "not_owner",
                    "required_role": min_role,
                    "current_role": current_role,
                    "session_owner": owner,
                },
            )

        # Own-session privilege: caller satisfies the action even
        # though their role is below ``min_role``. We surface the
        # *effective* role they need to satisfy the policy (the
        # ``min_role``) so downstream consumers reading
        # ``request.state.role`` don't see a confusing demoted value
        # that contradicts the action being allowed.
        request.state.role = min_role
        return min_role

    return _check
