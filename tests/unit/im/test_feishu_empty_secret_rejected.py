"""Plan 7 Task 12 (D7.16) — empty webhook secret must NOT silently pass.

The original ``router.py`` had a ``if secret and not verify(...)``
guard, which meant an unset secret silently accepted every callback.
The Plan 7 contract reverses that: ``FeishuBackend.verify_webhook``
returns ``False`` whenever the configured secret is empty.

These tests exercise the backend method directly (no FastAPI app, no
event loop wiring) so the security guarantee is asserted at the level
that operators inspect when auditing the codebase.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

import pytest
from pydantic import SecretStr

from gg_relay.config import Config
from gg_relay.im.backends.feishu import FeishuBackend, verify_feishu_signature

WEBHOOK_SECRET = "whk-secret-xyz"


def _cfg(*, webhook_secret: str | None) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.feishu_app_id = SecretStr("cli_app_xxx")
    cfg.feishu_app_secret = SecretStr("secret_xxx")
    cfg.feishu_webhook_secret = (
        SecretStr(webhook_secret) if webhook_secret is not None else None
    )
    return cfg


def _expected_signature(timestamp: str, secret: str) -> str:
    key = f"{timestamp}\n{secret}".encode()
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()


@pytest.mark.asyncio
async def test_empty_secret_returns_false():
    """Backends with no webhook secret MUST reject every callback.

    Plan 7 D7.16 forbids silent pass-through; an operator who forgot
    to set ``feishu_webhook_secret`` should see 401s, not implicit
    trust of unauthenticated callbacks.
    """
    backend = FeishuBackend(config=_cfg(webhook_secret=""))
    try:
        ok = await backend.verify_webhook(
            headers={
                "X-Lark-Request-Timestamp": "1700000000",
                "X-Lark-Signature": "anything-at-all",
            },
            body=b'{"type":"url_verification","challenge":"x"}',
        )
        assert ok is False
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_unset_secret_returns_false():
    """``None`` secret behaves identically to the empty string."""
    backend = FeishuBackend(config=_cfg(webhook_secret=None))
    try:
        ok = await backend.verify_webhook(
            headers={
                "X-Lark-Request-Timestamp": "1700000000",
                "X-Lark-Signature": "anything",
            },
            body=b"",
        )
        assert ok is False
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_correct_secret_returns_true():
    """A properly-signed callback with a configured secret → True."""
    backend = FeishuBackend(config=_cfg(webhook_secret=WEBHOOK_SECRET))
    try:
        ts = "1700000123"
        sig = _expected_signature(ts, WEBHOOK_SECRET)
        # Sanity-check the helper before we lean on it.
        assert verify_feishu_signature(
            timestamp=ts, secret=WEBHOOK_SECRET, received=sig
        )
        ok = await backend.verify_webhook(
            headers={
                "X-Lark-Request-Timestamp": ts,
                "X-Lark-Signature": sig,
            },
            body=b'{"action":{}}',
        )
        assert ok is True
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_wrong_signature_returns_false():
    """Configured secret + bad signature → False (no accidental pass)."""
    backend = FeishuBackend(config=_cfg(webhook_secret=WEBHOOK_SECRET))
    try:
        ok = await backend.verify_webhook(
            headers={
                "X-Lark-Request-Timestamp": "1700000456",
                "X-Lark-Signature": "definitely-not-the-right-hmac",
            },
            body=b"{}",
        )
        assert ok is False
    finally:
        await backend.aclose()
