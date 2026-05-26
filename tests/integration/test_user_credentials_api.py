# ruff: noqa: E501 — the test coverage table below intentionally exceeds
# the 100-char limit so each row reads as a single grep-able line.
"""``/me/credentials`` + ``/admin/credentials`` end-to-end — Plan v3 §B.8.3.

Drives the live FastAPI app via ``httpx.AsyncClient``. Pins the
plaintext-never-leaked, allowlist-enforced-on-both-routes, and
admin-override-attributes-correctly invariants.

Test coverage map (Plan v3 §B.8.3):

| ID | Test                                                                   | What it pins |
|----|------------------------------------------------------------------------|---|
| a  | test_me_put_then_list_returns_metadata_no_value                        | self PUT → list returns metadata only |
| b  | test_me_delete_idempotent                                              | second delete is no-op (204) |
| c  | test_me_put_rejects_disallowed_env_name                                | LD_PRELOAD on /me/ → 400 env_name_not_allowed |
| d  | test_viewer_role_blocked_from_me_routes                                | viewer cannot read or write own creds |
| e  | test_submitter_cannot_access_admin_routes                              | submitter+ but not admin → 403 on /admin/credentials |
| f  | test_admin_can_list_all_credentials                                    | /admin/credentials returns rows from multiple users |
| g  | test_admin_put_records_admin_as_created_by_label                       | overwriting bob's row leaves created_by_label='admin' |
| h  | test_admin_get_bricked_returns_only_mismatched_rows                    | rotated-key surfaces bricked rows |
| i  | test_value_never_appears_in_response_body_or_caplog                    | plaintext never echoed |
| j  | test_admin_put_rejects_ld_preload                                      | v2-Santa critical: admin route enforces allowlist for LD_PRELOAD |
| k  | test_admin_put_rejects_path                                            | v2-Santa critical: admin route enforces allowlist for PATH |
| l  | test_feature_disabled_returns_503                                      | missing encryption key → 503 user_credentials_disabled |
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.store import create_all_tables, make_async_engine


def _make_cfg(
    tmp_path: Path,
    *,
    api_keys_raw: str,
    role_mapping_raw: str,
    encryption_key: str | None,
) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/user-creds-e2e.db"
    cfg.api_keys_raw = api_keys_raw
    cfg.role_mapping_raw = role_mapping_raw
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://localhost:8000"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    # Hermetic: ALWAYS overwrite this field — even with None — so the
    # test doesn't accidentally inherit a value pydantic-settings auto-
    # loaded from the developer's repo-root ``.env`` file. The previous
    # ``if not None`` form treated ``None`` as "don't override", which
    # made the ``encryption_key=None`` branch only test "feature
    # disabled" on machines with an empty ``.env``.
    from pydantic import SecretStr

    cfg.credentials_encryption_key = (
        SecretStr(encryption_key) if encryption_key is not None else None
    )
    return cfg


@pytest_asyncio.fixture
async def app_factory(
    tmp_path: Path,
) -> AsyncIterator[Callable[..., Any]]:
    clients: list[Any] = []

    async def _make(
        api_keys_raw: str = (
            "admin-key:admin,"
            "alice-key:alice,"
            "bob-key:bob,"
            "viewer-key:viewer"
        ),
        role_mapping_raw: str = (
            "admin=admin,alice=submitter,bob=submitter,viewer=viewer"
        ),
        encryption_key: str | None = Fernet.generate_key().decode("utf-8"),
    ) -> AsyncClient:
        cfg = _make_cfg(
            tmp_path,
            api_keys_raw=api_keys_raw,
            role_mapping_raw=role_mapping_raw,
            encryption_key=encryption_key,
        )
        eng = make_async_engine(cfg.database_url)
        await create_all_tables(eng)
        await eng.dispose()
        app = create_app(cfg)
        transport = ASGITransport(app=app)
        client_ctx = AsyncClient(transport=transport, base_url="http://test")
        lifespan_ctx = app.router.lifespan_context(app)
        await lifespan_ctx.__aenter__()
        client = await client_ctx.__aenter__()
        clients.append((client_ctx, lifespan_ctx))
        return client

    yield _make

    for client_ctx, lifespan_ctx in clients:
        await client_ctx.__aexit__(None, None, None)
        await lifespan_ctx.__aexit__(None, None, None)


# ── /me/credentials — happy paths + viewer guard ───────────────────────


@pytest.mark.asyncio
async def test_me_put_then_list_returns_metadata_no_value(app_factory):
    """[a] PUT my own ANTHROPIC_API_KEY, then list returns metadata only.

    Pins the contract that ``value_encrypted`` and plaintext are
    NEVER in the JSON response.
    """
    client: AsyncClient = await app_factory()
    secret = "sk-must-never-leak-12345"
    r = await client.put(
        "/api/v1/me/credentials/ANTHROPIC_API_KEY",
        headers={"X-API-Key": "alice-key"},
        json={"value": secret, "notes": "first"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["env_name"] == "ANTHROPIC_API_KEY"
    assert body["user_label"] == "alice"
    assert "value" not in body
    assert "value_encrypted" not in body
    assert secret not in r.text

    r = await client.get(
        "/api/v1/me/credentials",
        headers={"X-API-Key": "alice-key"},
    )
    assert r.status_code == 200, r.text
    listing = r.json()
    assert listing["user_label"] == "alice"
    assert len(listing["credentials"]) == 1
    assert listing["credentials"][0]["env_name"] == "ANTHROPIC_API_KEY"
    assert secret not in r.text


@pytest.mark.asyncio
async def test_me_delete_idempotent(app_factory):
    """[b] second DELETE is a no-op (204)."""
    client: AsyncClient = await app_factory()
    await client.put(
        "/api/v1/me/credentials/ANTHROPIC_API_KEY",
        headers={"X-API-Key": "alice-key"},
        json={"value": "sk-x"},
    )
    r1 = await client.delete(
        "/api/v1/me/credentials/ANTHROPIC_API_KEY",
        headers={"X-API-Key": "alice-key"},
    )
    assert r1.status_code == 204, r1.text
    r2 = await client.delete(
        "/api/v1/me/credentials/ANTHROPIC_API_KEY",
        headers={"X-API-Key": "alice-key"},
    )
    assert r2.status_code == 204, r2.text


@pytest.mark.asyncio
async def test_me_put_rejects_disallowed_env_name(app_factory):
    """[c] PUT LD_PRELOAD on /me/ route returns 400 env_name_not_allowed."""
    client: AsyncClient = await app_factory()
    r = await client.put(
        "/api/v1/me/credentials/LD_PRELOAD",
        headers={"X-API-Key": "alice-key"},
        json={"value": "/tmp/evil.so"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "env_name_not_allowed"


@pytest.mark.asyncio
async def test_viewer_role_blocked_from_me_routes(app_factory):
    """[d] viewer cannot list or PUT own creds (the routes are
    submitter+). Storing a credential you cannot use is pointless,
    so the role boundary matches the use case."""
    client: AsyncClient = await app_factory()
    r = await client.get(
        "/api/v1/me/credentials",
        headers={"X-API-Key": "viewer-key"},
    )
    assert r.status_code == 403, r.text
    r = await client.put(
        "/api/v1/me/credentials/ANTHROPIC_API_KEY",
        headers={"X-API-Key": "viewer-key"},
        json={"value": "sk-x"},
    )
    assert r.status_code == 403, r.text


# ── /admin/credentials — RBAC + admin override ─────────────────────────


@pytest.mark.asyncio
async def test_submitter_cannot_access_admin_routes(app_factory):
    """[e] alice (submitter) sees 403 on every /admin/credentials route."""
    client: AsyncClient = await app_factory()
    for method, path in [
        ("GET", "/api/v1/admin/credentials"),
        ("GET", "/api/v1/admin/credentials/bricked"),
        ("PUT", "/api/v1/admin/credentials/bob/ANTHROPIC_API_KEY"),
        ("DELETE", "/api/v1/admin/credentials/bob/ANTHROPIC_API_KEY"),
    ]:
        r = await client.request(
            method,
            path,
            headers={"X-API-Key": "alice-key"},
            json={"value": "x"} if method == "PUT" else None,
        )
        assert r.status_code == 403, (
            f"{method} {path} should be admin-only; got {r.status_code}"
        )


@pytest.mark.asyncio
async def test_admin_can_list_all_credentials(app_factory):
    """[f] /admin/credentials returns rows across every user."""
    client: AsyncClient = await app_factory()
    await client.put(
        "/api/v1/me/credentials/ANTHROPIC_API_KEY",
        headers={"X-API-Key": "alice-key"},
        json={"value": "sk-alice"},
    )
    await client.put(
        "/api/v1/me/credentials/ANTHROPIC_API_KEY",
        headers={"X-API-Key": "bob-key"},
        json={"value": "sk-bob"},
    )
    r = await client.get(
        "/api/v1/admin/credentials",
        headers={"X-API-Key": "admin-key"},
    )
    assert r.status_code == 200, r.text
    labels = {row["user_label"] for row in r.json()["credentials"]}
    assert "alice" in labels
    assert "bob" in labels


@pytest.mark.asyncio
async def test_admin_put_records_admin_as_created_by_label(app_factory):
    """[g] Admin overrides bob's row → ``created_by_label`` is admin's label.

    The dashboard surfaces this so bob can see when an admin
    touched one of his rows (audit transparency).
    """
    client: AsyncClient = await app_factory()
    await client.put(
        "/api/v1/me/credentials/ANTHROPIC_API_KEY",
        headers={"X-API-Key": "bob-key"},
        json={"value": "sk-bob-self"},
    )
    r = await client.put(
        "/api/v1/admin/credentials/bob/ANTHROPIC_API_KEY",
        headers={"X-API-Key": "admin-key"},
        json={"value": "sk-set-by-admin", "notes": "rotated by ops"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_label"] == "bob"
    assert body["created_by_label"] == "admin"
    assert body["notes"] == "rotated by ops"

    # Bob's listing now shows the admin-touched row.
    r = await client.get(
        "/api/v1/me/credentials",
        headers={"X-API-Key": "bob-key"},
    )
    assert r.status_code == 200
    assert r.json()["credentials"][0]["created_by_label"] == "admin"


@pytest.mark.asyncio
async def test_admin_get_bricked_returns_only_mismatched_rows(app_factory):
    """[h] /admin/credentials/bricked surfaces rows from a stale key.

    We simulate a "stale" row by booting the app with key A,
    seeding a row, then asking the bricked endpoint — at this point
    there are no bricked rows. Then we reboot with key B (without
    re-encrypting the row) and verify it appears bricked.
    """
    key_a = Fernet.generate_key().decode("utf-8")
    client_a: AsyncClient = await app_factory(encryption_key=key_a)
    await client_a.put(
        "/api/v1/me/credentials/ANTHROPIC_API_KEY",
        headers={"X-API-Key": "alice-key"},
        json={"value": "sk-x"},
    )
    r = await client_a.get(
        "/api/v1/admin/credentials/bricked",
        headers={"X-API-Key": "admin-key"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["credentials"] == []


# ── plaintext discipline ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_value_never_appears_in_response_body_or_caplog(
    app_factory, caplog
):
    """[i] No route, no log message, EVER includes the raw value.

    Defense in depth on top of the store-level metadata projection.
    """
    client: AsyncClient = await app_factory()
    secret = "sk-NEVER-LEAK-via-router-or-log-12345abcde"
    with caplog.at_level("DEBUG"):
        r1 = await client.put(
            "/api/v1/me/credentials/ANTHROPIC_API_KEY",
            headers={"X-API-Key": "alice-key"},
            json={"value": secret},
        )
        r2 = await client.get(
            "/api/v1/me/credentials",
            headers={"X-API-Key": "alice-key"},
        )
        r3 = await client.get(
            "/api/v1/admin/credentials",
            headers={"X-API-Key": "admin-key"},
        )
    assert secret not in r1.text
    assert secret not in r2.text
    assert secret not in r3.text
    for record in caplog.records:
        assert secret not in record.getMessage(), (
            "router or middleware leaked the plaintext value to logs"
        )


# ── v2-Santa critical regression net: admin route enforces allowlist ───


@pytest.mark.asyncio
async def test_admin_put_rejects_ld_preload(app_factory):
    """[j] PUT LD_PRELOAD on /admin/credentials/{user}/LD_PRELOAD → 400.

    Plan v3 §B.5 — the v2-Santa-reviewer-flagged critical: admin
    is NOT trusted to set LD_PRELOAD (would load a malicious .so
    into every spawned ``claude`` subprocess).
    """
    client: AsyncClient = await app_factory()
    r = await client.put(
        "/api/v1/admin/credentials/bob/LD_PRELOAD",
        headers={"X-API-Key": "admin-key"},
        json={"value": "/tmp/evil.so"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "env_name_not_allowed"


@pytest.mark.asyncio
async def test_admin_put_rejects_path(app_factory):
    """[k] PUT PATH on admin route → 400.

    Plan v3 §B.5 — same family as LD_PRELOAD: PATH manipulation
    would let an admin redirect ``claude`` subprocess invocations
    to an attacker-supplied binary.
    """
    client: AsyncClient = await app_factory()
    r = await client.put(
        "/api/v1/admin/credentials/bob/PATH",
        headers={"X-API-Key": "admin-key"},
        json={"value": "/tmp/evil-bin:/usr/bin"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "env_name_not_allowed"


# ── feature-disabled gate ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feature_disabled_returns_503(app_factory):
    """[l] Missing encryption key → every route returns 503 with the
    documented ``user_credentials_disabled`` code so clients can
    show a calibrated UX prompt."""
    client: AsyncClient = await app_factory(encryption_key=None)
    for method, path in [
        ("GET", "/api/v1/me/credentials"),
        ("PUT", "/api/v1/me/credentials/ANTHROPIC_API_KEY"),
        ("DELETE", "/api/v1/me/credentials/ANTHROPIC_API_KEY"),
        ("GET", "/api/v1/admin/credentials"),
        ("PUT", "/api/v1/admin/credentials/bob/ANTHROPIC_API_KEY"),
        ("DELETE", "/api/v1/admin/credentials/bob/ANTHROPIC_API_KEY"),
    ]:
        headers = {
            "X-API-Key": (
                "admin-key" if "/admin/" in path else "alice-key"
            )
        }
        r = await client.request(
            method,
            path,
            headers=headers,
            json={"value": "x"} if method == "PUT" else None,
        )
        assert r.status_code == 503, (
            f"{method} {path} should 503 when feature disabled; got {r.status_code} body={r.text}"
        )
        assert r.json()["detail"]["code"] == "user_credentials_disabled"
