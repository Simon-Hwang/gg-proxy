"""Integration tests for /im/feishu/callback."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.im.router import verify_feishu_signature

WEBHOOK_SECRET = "whk-test-secret"


def _sig_for(ts: str, secret: str) -> str:
    key = f"{ts}\n{secret}".encode()
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()


def _cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/fw.db"
    cfg.api_keys_raw = "k1"
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.feishu_webhook_secret = SecretStr(WEBHOOK_SECRET)
    cfg.dashboard_admin_password = SecretStr("admin")
    cfg.dashboard_session_secret = SecretStr("x" * 32)
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def client(tmp_path: Path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    from gg_relay.store import create_all_tables, make_async_engine

    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as ac, app.router.lifespan_context(app):
        yield ac, app


class TestSignatureFunction:
    """``verify_feishu_signature`` is a pure helper — test it directly."""

    def test_known_vector_matches(self):
        ts = "1700000000"
        ok = _sig_for(ts, WEBHOOK_SECRET)
        assert verify_feishu_signature(
            timestamp=ts, secret=WEBHOOK_SECRET, received=ok
        )

    def test_wrong_signature_rejected(self):
        assert not verify_feishu_signature(
            timestamp="123", secret=WEBHOOK_SECRET, received="not-base64"
        )

    def test_missing_signature_rejected(self):
        assert not verify_feishu_signature(
            timestamp="123", secret=WEBHOOK_SECRET, received=None
        )


class TestWebhookCallback:
    async def test_bad_signature_returns_401(self, client):
        ac, _ = client
        r = await ac.post(
            "/im/feishu/callback",
            content=b"{}",
            headers={
                "X-Lark-Request-Timestamp": "1700000000",
                "X-Lark-Signature": "wrong",
            },
        )
        assert r.status_code == 401

    async def test_url_verification_challenge_echoed(self, client):
        ac, _ = client
        body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
        ts = "1700000001"
        sig = _sig_for(ts, WEBHOOK_SECRET)
        r = await ac.post(
            "/im/feishu/callback",
            content=body,
            headers={
                "X-Lark-Request-Timestamp": ts,
                "X-Lark-Signature": sig,
            },
        )
        assert r.status_code == 200
        assert r.json() == {"challenge": "abc123"}

    async def test_unknown_req_id_returns_already_resolved(self, client):
        ac, _ = client
        body = json.dumps(
            {
                "action": {
                    "value": {
                        "session_id": "s1",
                        "req_id": "rzzz",
                        "decision": "accept",
                    }
                },
                "operator": {"open_id": "u-1"},
            }
        ).encode()
        ts = "1700000002"
        sig = _sig_for(ts, WEBHOOK_SECRET)
        r = await ac.post(
            "/im/feishu/callback",
            content=body,
            headers={
                "X-Lark-Request-Timestamp": ts,
                "X-Lark-Signature": sig,
            },
        )
        assert r.status_code == 200
        assert r.json() == {
            "toast": {"type": "info", "content": "already resolved"}
        }

    async def test_malformed_json_400(self, client):
        ac, _ = client
        ts = "1700000003"
        sig = _sig_for(ts, WEBHOOK_SECRET)
        r = await ac.post(
            "/im/feishu/callback",
            content=b"{not-json",
            headers={
                "X-Lark-Request-Timestamp": ts,
                "X-Lark-Signature": sig,
            },
        )
        assert r.status_code == 400

    async def test_missing_action_value_400(self, client):
        ac, _ = client
        ts = "1700000004"
        sig = _sig_for(ts, WEBHOOK_SECRET)
        r = await ac.post(
            "/im/feishu/callback",
            content=json.dumps({"action": {}}).encode(),
            headers={
                "X-Lark-Request-Timestamp": ts,
                "X-Lark-Signature": sig,
            },
        )
        assert r.status_code == 400

    async def test_valid_resolve_dispatches_to_coordinator(self, client):
        ac, app = client
        # Pre-register a HITL request directly on the coordinator.
        coord = app.state.coordinator
        import asyncio

        task = asyncio.create_task(
            coord.request(
                "sIM:r0",
                session_id="sIM",
                tool="WriteFile",
                args={"path": "/tmp/x"},
            )
        )
        await asyncio.sleep(0.01)
        ts = "1700000005"
        sig = _sig_for(ts, WEBHOOK_SECRET)
        body = json.dumps(
            {
                "action": {
                    "value": {
                        "session_id": "sIM",
                        "req_id": "sIM:r0",
                        "decision": "accept",
                    }
                },
                "operator": {"open_id": "u-1"},
            }
        ).encode()
        r = await ac.post(
            "/im/feishu/callback",
            content=body,
            headers={
                "X-Lark-Request-Timestamp": ts,
                "X-Lark-Signature": sig,
            },
        )
        assert r.status_code == 200
        assert r.json()["toast"]["type"] == "success"
        decision = await asyncio.wait_for(task, timeout=1)
        assert decision == "accept"
