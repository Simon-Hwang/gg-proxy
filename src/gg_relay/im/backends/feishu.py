"""Feishu (Lark) IM backend.

Sends interactive messages via ``open-apis/im/v1/messages`` and caches
``tenant_access_token`` for its TTL. The actionable card carries two
buttons; the ``value`` payload is round-tripped to our webhook router so
the resolver dispatch is signed and idempotent.

This backend is intentionally lean — only HTTPX is required (no SDK).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from gg_relay.config import Config


@dataclass
class _TokenCache:
    token: str = ""
    expires_at: float = 0.0


@dataclass
class FeishuBackend:
    """Backend for Feishu interactive cards.

    The constructor takes a :class:`Config` rather than individual fields
    so test fixtures can pass a fully-formed config; HTTP timeouts are
    intentionally short (Plan §6 Task 10 uses 30s).
    """

    config: Config
    base_url: str = "https://open.feishu.cn"
    http: httpx.AsyncClient = field(init=False)
    name: str = "feishu"
    _token: _TokenCache = field(default_factory=_TokenCache, repr=False)

    def __post_init__(self) -> None:
        self.http = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _tenant_token(self) -> str:
        """Fetch + cache the tenant access token (~ 2h TTL)."""
        now = time.time()
        if self._token.token and self._token.expires_at > now + 60:
            return self._token.token
        app_id = (
            self.config.feishu_app_id.get_secret_value()
            if self.config.feishu_app_id
            else ""
        )
        secret = (
            self.config.feishu_app_secret.get_secret_value()
            if self.config.feishu_app_secret
            else ""
        )
        if not app_id or not secret:
            raise RuntimeError("feishu_app_id / feishu_app_secret unset")
        r = await self.http.post(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": secret},
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("code") != 0:
            raise RuntimeError(
                f"feishu token error: {payload.get('msg')!r}"
            )
        self._token = _TokenCache(
            token=payload["tenant_access_token"],
            expires_at=now + payload.get("expire", 7200),
        )
        return self._token.token

    def build_hitl_card(
        self,
        *,
        session_id: str,
        req_id: str,
        tool: str,
        args_summary: str,
    ) -> dict[str, Any]:
        """Construct the interactive-card payload (no HTTP)."""
        body = args_summary[:512]
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"HITL: {tool}"},
                "template": "yellow",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**Session**: `{session_id}`\n"
                        f"**req_id**: `{req_id}`\n"
                        f"**Args**:\n```\n{body}\n```"
                    ),
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "Approve",
                            },
                            "type": "primary",
                            "value": {
                                "session_id": session_id,
                                "req_id": req_id,
                                "decision": "accept",
                            },
                        },
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "Deny",
                            },
                            "type": "danger",
                            "value": {
                                "session_id": session_id,
                                "req_id": req_id,
                                "decision": "deny",
                            },
                        },
                    ],
                },
            ],
        }

    async def notify_hitl_pending(
        self,
        *,
        session_id: str,
        req_id: str,
        tool: str,
        args_summary: str,
        callback_base: str,
    ) -> None:
        del callback_base  # cards carry the session+req_id in their value
        token = await self._tenant_token()
        card = self.build_hitl_card(
            session_id=session_id,
            req_id=req_id,
            tool=tool,
            args_summary=args_summary,
        )
        target = self.config.feishu_target_chat_id or ""
        await self.http.post(
            "/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": target,
                "msg_type": "interactive",
                "content": json.dumps(card),
            },
        )

    async def notify_session_end(
        self,
        *,
        session_id: str,
        status: str,
        summary: str,
    ) -> None:
        token = await self._tenant_token()
        target = self.config.feishu_target_chat_id or ""
        await self.http.post(
            "/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": target,
                "msg_type": "text",
                "content": json.dumps(
                    {"text": f"[{status}] {session_id}\n{summary[:512]}"}
                ),
            },
        )
