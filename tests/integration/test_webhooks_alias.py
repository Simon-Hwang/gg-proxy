"""Plan 7 Task 12 (D7.16) — canonical + deprecated-alias webhook paths.

Both ``POST /api/v1/webhooks/feishu`` (canonical) and the legacy
``POST /im/feishu/callback`` (alias) MUST accept a valid signature
and reject an invalid one. The alias MUST additionally surface the
``Deprecation: true`` header plus an RFC 8288 ``Link`` pointing at
the successor URL — even on the 401 path, so operators driving the
old endpoint notice the warning even when their requests fail.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config

WEBHOOK_SECRET = "whk-alias-test"
API_KEY = "k1"
CANONICAL = "/api/v1/webhooks/feishu"
ALIAS = "/im/feishu/callback"


def _sig(ts: str, secret: str) -> str:
    key = f"{ts}\n{secret}".encode()
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()


def _cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/alias.db"
    cfg.api_keys_raw = API_KEY
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.feishu_webhook_secret = SecretStr(WEBHOOK_SECRET)
    cfg.dashboard_admin_password = SecretStr("admin")
    cfg.dashboard_session_secret = SecretStr("x" * 32)
    cfg.grace_period_s = 1
    return cfg


# NB: the canonical path lives UNDER ``/api/v1``, which currently
# triggers :class:`APIKeyAuthMiddleware`. Tests pass ``X-API-Key`` so
# the signature-check assertions exercise the webhook layer rather
# than the auth layer. Plan 7 Task 11 owns the route-exempt mechanism
# that lets Feishu (which doesn't speak X-API-Key) reach the route in
# production — until that lands, operators MUST proxy callbacks
# through a layer that injects the header.


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


@pytest.mark.asyncio
class TestCanonicalPath:
    async def test_canonical_path_accepts_valid_signature(self, client):
        """``/api/v1/webhooks/feishu`` accepts a properly-signed payload."""
        ac, _ = client
        ts = "1700001000"
        body = json.dumps(
            {"type": "url_verification", "challenge": "c1"}
        ).encode()
        r = await ac.post(
            CANONICAL,
            content=body,
            headers={
                "X-API-Key": API_KEY,
                "X-Lark-Request-Timestamp": ts,
                "X-Lark-Signature": _sig(ts, WEBHOOK_SECRET),
            },
        )
        assert r.status_code == 200
        assert r.json() == {"challenge": "c1"}
        # Canonical responses MUST NOT carry the deprecation hint.
        assert "deprecation" not in {k.lower() for k in r.headers}

    async def test_canonical_path_bad_signature_401(self, client):
        ac, _ = client
        r = await ac.post(
            CANONICAL,
            content=b"{}",
            headers={
                "X-API-Key": API_KEY,
                "X-Lark-Request-Timestamp": "1700001001",
                "X-Lark-Signature": "wrong",
            },
        )
        assert r.status_code == 401


@pytest.mark.asyncio
class TestAliasPath:
    async def test_alias_path_accepts_valid_signature(self, client):
        """``/im/feishu/callback`` still works AND emits Deprecation hints."""
        ac, _ = client
        ts = "1700001002"
        body = json.dumps(
            {"type": "url_verification", "challenge": "c2"}
        ).encode()
        r = await ac.post(
            ALIAS,
            content=body,
            headers={
                "X-Lark-Request-Timestamp": ts,
                "X-Lark-Signature": _sig(ts, WEBHOOK_SECRET),
            },
        )
        assert r.status_code == 200
        assert r.json() == {"challenge": "c2"}
        assert r.headers.get("Deprecation") == "true"
        link = r.headers.get("Link", "")
        assert "/api/v1/webhooks/feishu" in link
        assert 'rel="successor-version"' in link

    async def test_alias_deprecation_header_on_401(self, client):
        """Deprecation header must surface even on failing signatures."""
        ac, _ = client
        r = await ac.post(
            ALIAS,
            content=b"{}",
            headers={
                "X-Lark-Request-Timestamp": "1700001003",
                "X-Lark-Signature": "wrong",
            },
        )
        assert r.status_code == 401
        assert r.headers.get("Deprecation") == "true"
        assert "/api/v1/webhooks/feishu" in r.headers.get("Link", "")


@pytest.mark.asyncio
async def test_invalid_signature_401_both_paths(client):
    """Same bad-signature payload → 401 on both canonical and alias routes."""
    ac, _ = client
    bad_headers = {
        "X-API-Key": API_KEY,
        "X-Lark-Request-Timestamp": "1700001004",
        "X-Lark-Signature": "definitely-not-valid",
    }
    r_canonical = await ac.post(CANONICAL, content=b"{}", headers=bad_headers)
    r_alias = await ac.post(ALIAS, content=b"{}", headers=bad_headers)
    assert r_canonical.status_code == 401
    assert r_alias.status_code == 401
    # Alias keeps its deprecation hints on the 401 path.
    assert r_alias.headers.get("Deprecation") == "true"


@pytest.mark.asyncio
async def test_canonical_path_in_openapi(client):
    """The canonical path is discoverable via the OpenAPI document."""
    ac, _ = client
    # /openapi.json is unauthenticated (not under /api/v1) — no API key
    # needed to fetch the schema.
    r = await ac.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json().get("paths", {})
    assert CANONICAL in paths
    # Alias is still present but marked deprecated.
    assert ALIAS in paths
    assert paths[ALIAS]["post"].get("deprecated") is True
