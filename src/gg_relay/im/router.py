"""Webhook router for IM callbacks.

Plan 7 Task 12 (D7.16) introduced a two-route layout:

* canonical ``POST /api/v1/webhooks/feishu`` — the path 0.8+ documents
* alias    ``POST /im/feishu/callback``      — deprecated, kept for the
  0.7/0.8 migration so existing Feishu bot configurations don't break

Both routes share :func:`_process_feishu_callback` so signature checks,
URL-verification handshakes, malformed-payload handling, and HITL
dispatch behave identically. The alias responds with a ``Deprecation:
true`` header plus an RFC 8288 ``Link`` pointing at the canonical
route; 0.8.0 will delete the alias entirely.

NOTE (Plan 7 Task 11 coupling): the canonical path sits UNDER
``/api/v1``, which today triggers :class:`APIKeyAuthMiddleware`.
Feishu does not (and cannot) send ``X-API-Key`` on callbacks, so
Task 11 is responsible for adding a route-exempt mechanism that
lets ``/api/v1/webhooks/*`` bypass the API-key check. Until that
lands, operators MUST proxy callbacks through a layer that injects
the header — or pin Feishu to the alias path, which is intentionally
hosted outside ``/api/v1`` for exactly this reason.

Signature verification is delegated to the IMBackend that lives on
``request.app.state.im_backend``. That backend is constructed by the
lifespan whenever ``feishu_webhook_secret`` is set (even without full
send credentials) so the inbound path stays operational on read-only
deployments. ``verify_feishu_signature`` is re-exported from
:mod:`gg_relay.im.backends.feishu` for tests and any downstream code
that constructed expected-signature vectors by hand.
"""
from __future__ import annotations

import json
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request, Response

from gg_relay.api.deps import CoordinatorDep
from gg_relay.im.backends.feishu import verify_feishu_signature
from gg_relay.im.protocol import IMBackend
from gg_relay.session.hitl.coordinator import HITLCoordinator, HITLNotPending

__all__ = ["router", "verify_feishu_signature"]

router = APIRouter(tags=["im-feishu"])


def _get_feishu_backend(request: Request) -> IMBackend:
    """Resolve the Feishu backend from app.state, 503 if absent.

    The lifespan wires ``app.state.im_backend`` whenever the deployment
    has either send creds (``feishu_app_id`` + ``feishu_app_secret``)
    or just a webhook secret. Missing it here means the operator never
    configured Feishu at all — that's a server config issue, not a
    bad-request signal, so we surface 503.
    """
    backend = getattr(request.app.state, "im_backend", None)
    if backend is None:
        raise HTTPException(status_code=503, detail="im backend not configured")
    return cast(IMBackend, backend)


async def _process_feishu_callback(
    request: Request,
    backend: IMBackend,
    coordinator: HITLCoordinator,
) -> dict[str, Any]:
    body = await request.body()
    # Starlette's ``Headers`` is a case-insensitive Mapping[str, str];
    # we hand it to the backend verbatim so verify_webhook can look up
    # either ``X-Lark-Signature`` or ``x-lark-signature`` without the
    # router caring how the upstream proxy normalised the casing. A
    # ``dict(request.headers)`` here would lowercase the keys and break
    # title-case lookups in backend implementations.
    if not await backend.verify_webhook(request.headers, body):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="malformed payload") from exc

    # Feishu's URL-verification challenge handshake.
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}

    action = payload.get("action") or {}
    value = action.get("value") or {}
    sid = value.get("session_id")
    rid = value.get("req_id")
    decision_raw = value.get("decision")
    if not sid or not rid or decision_raw not in {"accept", "deny"}:
        raise HTTPException(status_code=400, detail="missing action fields")

    user = payload.get("operator", {}).get("open_id", "unknown")
    full_req_id = rid if ":" in rid else f"{sid}:{rid}"
    decision = cast(Any, decision_raw)
    reason = f"im_approval:feishu:{user}"
    try:
        await coordinator.resolve(full_req_id, decision, reason=reason)
        return {"toast": {"type": "success", "content": f"{decision} recorded"}}
    except HITLNotPending:
        return {"toast": {"type": "info", "content": "already resolved"}}


@router.post("/api/v1/webhooks/feishu")
async def feishu_webhook_canonical(
    request: Request,
    coordinator: HITLCoordinator = CoordinatorDep,
) -> dict[str, Any]:
    """Canonical Feishu interactive-callback receiver (Plan 7 D7.16).

    Verifies the signature via the configured IMBackend, then routes
    HITL decisions to the coordinator. Returns 401 on signature
    failure (including unset webhook secret — see
    :meth:`FeishuBackend.verify_webhook`).
    """
    backend = _get_feishu_backend(request)
    return await _process_feishu_callback(request, backend, coordinator)


_ALIAS_DEPRECATION_HEADERS = {
    "Deprecation": "true",
    "Link": '</api/v1/webhooks/feishu>; rel="successor-version"',
}


@router.post("/im/feishu/callback", deprecated=True)
async def feishu_webhook_alias(
    request: Request,
    response: Response,
    coordinator: HITLCoordinator = CoordinatorDep,
) -> dict[str, Any]:
    """Deprecated alias for the canonical Feishu callback path.

    Behaviour is identical to ``/api/v1/webhooks/feishu`` but every
    response carries a ``Deprecation: true`` header and an RFC 8288
    ``Link`` pointing at the successor URL. 0.8.0 will remove this
    route — operators MUST update their Feishu bot configuration to
    the canonical path before upgrading.

    Error responses (401 bad-signature, 400 malformed payload) also
    surface the deprecation hints so operators driving against this
    alias notice the warning even when their requests fail.
    """
    backend = _get_feishu_backend(request)
    try:
        result = await _process_feishu_callback(request, backend, coordinator)
    except HTTPException as exc:
        merged_headers = {**(exc.headers or {}), **_ALIAS_DEPRECATION_HEADERS}
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=merged_headers,
        ) from exc
    for k, v in _ALIAS_DEPRECATION_HEADERS.items():
        response.headers[k] = v
    return result
