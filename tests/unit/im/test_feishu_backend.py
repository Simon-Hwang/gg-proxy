"""Unit tests for :class:`FeishuBackend` (no real Feishu account needed).

All HTTP traffic is mocked with :pypi:`respx`.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from gg_relay.config import Config
from gg_relay.im.backends.feishu import FeishuBackend


def _cfg() -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.feishu_app_id = SecretStr("cli_app_xxx")
    cfg.feishu_app_secret = SecretStr("secret_xxx")
    cfg.feishu_target_chat_id = "oc_xxx"
    cfg.feishu_webhook_secret = SecretStr("whk-secret")
    return cfg


class TestCardConstruction:
    """The card payload must be the precise shape Feishu expects."""

    @pytest.mark.asyncio
    async def test_card_carries_two_buttons_with_action_value(self):
        backend = FeishuBackend(config=_cfg())
        try:
            card = backend.build_hitl_card(
                session_id="s1",
                req_id="s1:r0",
                tool="WriteFile",
                args_summary="path=/etc/passwd",
            )
        finally:
            await backend.aclose()
        action_block = next(e for e in card["elements"] if e["tag"] == "action")
        buttons = action_block["actions"]
        assert len(buttons) == 2
        decisions = sorted(b["value"]["decision"] for b in buttons)
        assert decisions == ["accept", "deny"]
        for b in buttons:
            assert b["value"]["session_id"] == "s1"
            assert b["value"]["req_id"] == "s1:r0"
        assert card["header"]["title"]["content"].endswith("WriteFile")


class TestTokenCacheAndNotify:
    @respx.mock
    @pytest.mark.asyncio
    async def test_tenant_token_fetched_and_cached(self):
        backend = FeishuBackend(config=_cfg())
        try:
            tok_route = respx.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "msg": "ok",
                        "tenant_access_token": "t-abc",
                        "expire": 7200,
                    },
                )
            )
            t1 = await backend._tenant_token()
            t2 = await backend._tenant_token()
            assert t1 == t2 == "t-abc"
            # Cached: only ONE network call.
            assert tok_route.call_count == 1
        finally:
            await backend.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_notify_hitl_pending_sends_interactive_message(self):
        backend = FeishuBackend(config=_cfg())
        try:
            respx.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "msg": "ok",
                        "tenant_access_token": "t-abc",
                        "expire": 7200,
                    },
                )
            )
            send_route = respx.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
            ).mock(
                return_value=httpx.Response(
                    200, json={"code": 0, "data": {"message_id": "om_x"}}
                )
            )
            await backend.notify_hitl_pending(
                session_id="s2",
                req_id="s2:r9",
                tool="ShellExec",
                args_summary="cmd=rm -rf /",
                callback_base="http://t",
            )
            assert send_route.called
            request = send_route.calls.last.request
            body = json.loads(request.content)
            assert body["receive_id"] == "oc_xxx"
            assert body["msg_type"] == "interactive"
            inner = json.loads(body["content"])
            assert inner["header"]["title"]["content"].endswith("ShellExec")
        finally:
            await backend.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_token_error_propagates(self):
        backend = FeishuBackend(config=_cfg())
        try:
            respx.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
            ).mock(
                return_value=httpx.Response(
                    200, json={"code": 99991663, "msg": "invalid app"}
                )
            )
            with pytest.raises(RuntimeError, match="feishu token error"):
                await backend._tenant_token()
        finally:
            await backend.aclose()
