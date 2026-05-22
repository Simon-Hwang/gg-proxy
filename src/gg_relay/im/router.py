"""Webhook router for IM callbacks.

Currently only Feishu is wired; the path is
``/im/feishu/callback`` and the body is the JSON payload sent by Feishu
when an operator clicks one of the HITL-card buttons.

Signature verification follows the Feishu doc:
``base64(hmac_sha256(key=secret, msg=timestamp + "\\n" + secret))``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request

from gg_relay.api.deps import CoordinatorDep
from gg_relay.session.hitl.coordinator import HITLCoordinator, HITLNotPending

router = APIRouter(prefix="/im/feishu", tags=["im-feishu"])


def verify_feishu_signature(
    *,
    timestamp: str,
    secret: str,
    received: str | None,
) -> bool:
    """Verify a Feishu webhook signature.

    The algorithm is HMAC-SHA256 with the signing key being
    ``timestamp + "\\n" + secret`` over an EMPTY message — that's the
    documented variant used for chatbot custom-bot signing, which is what
    interactive callbacks use today. We expose it as a pure function so
    tests can call it with hand-computed expected values.
    """
    if not received:
        return False
    key = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(key, b"", hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, received)


@router.post("/callback")
async def feishu_callback(
    request: Request,
    coordinator: HITLCoordinator = CoordinatorDep,
) -> dict[str, Any]:
    cfg = request.app.state.config
    secret = (
        cfg.feishu_webhook_secret.get_secret_value()
        if cfg.feishu_webhook_secret
        else ""
    )
    body = await request.body()
    timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
    signature = request.headers.get("X-Lark-Signature")
    if secret and not verify_feishu_signature(
        timestamp=timestamp, secret=secret, received=signature
    ):
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
