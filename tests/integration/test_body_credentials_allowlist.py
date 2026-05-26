# ruff: noqa: E501 — table comments intentionally over 100 chars for grep-ability.
"""POST /api/v1/sessions body.credentials allowlist — Plan v5 §3.2.

Defence-in-depth: ``body.credentials`` keys are validated against the
SAME ``ALLOWED_ENV_NAMES`` frozenset that ``/api/v1/me/credentials``
enforces on upload, REGARDLESS of strict mode. Closes the Santa-v2
wildcard-injection channel:

  Before:  POST /api/v1/sessions {"credentials": {"LD_PRELOAD": "..."}}
           → reaches manager → reaches SDK → poisons subprocess env
  After:   400 unsupported_credential_key BEFORE the manager is touched.

Test coverage map (Plan v5 §3.2):

| ID  | Test                                                            | What it pins |
|-----|-----------------------------------------------------------------|---|
| TB1 | test_unknown_body_key_rejected_400                              | LD_PRELOAD → 400 + manager.submit NOT called |
| TB2 | test_unknown_body_key_rejected_even_when_strict_off             | Defence-in-depth always on, regardless of strict flag |
| TB3 | test_allowed_body_key_accepted                                  | ANTHROPIC_API_KEY passes through |
| TB4 | test_mixed_keys_one_bad_rejects_all                             | Mixed valid+invalid → all-or-nothing rejection |
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from gg_relay.api.deps import get_manager
from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.session.manager import SessionDetail
from gg_relay.store import create_all_tables, make_async_engine


def _make_cfg(
    tmp_path: Path, *, require_per_user_credentials: bool = False
) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/body-allowlist.db"
    cfg.api_keys_raw = "alice-key:alice"
    cfg.role_mapping_raw = "alice=submitter"
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://localhost:8000"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    cfg.require_per_user_credentials = require_per_user_credentials
    from pydantic import SecretStr
    cfg.credentials_encryption_key = SecretStr(
        Fernet.generate_key().decode("utf-8")
    )
    return cfg


@pytest_asyncio.fixture
async def app_with_mock_manager(
    tmp_path: Path,
) -> AsyncIterator[Callable[..., Any]]:
    """Build a real app but swap ``manager`` via dependency override
    so we can assert ``manager.submit`` was/wasn't called.

    Critical for TB1/TB4 — the wildcard channel must be closed BEFORE
    the manager is touched; a behavioural test of "manager.submit
    not called" is the regression net.
    """
    state: list[tuple[Any, Any, AsyncMock]] = []

    async def _make(
        *, require_per_user_credentials: bool = False
    ) -> tuple[AsyncClient, AsyncMock]:
        cfg = _make_cfg(
            tmp_path,
            require_per_user_credentials=require_per_user_credentials,
        )
        eng = make_async_engine(cfg.database_url)
        await create_all_tables(eng)
        await eng.dispose()
        app = create_app(cfg)

        mock_manager = AsyncMock()
        mock_manager.submit = AsyncMock(return_value="mock-sid")
        # Stub a valid SessionDetail so the post-submit response build
        # doesn't crash with pydantic validation errors when TB3's
        # happy path reaches that code.
        from datetime import datetime, timezone

        from gg_relay.core import SessionState

        mock_manager.get = AsyncMock(
            return_value=SessionDetail(
                id="mock-sid",
                status=SessionState.QUEUED,
                spec_json={"prompt": "x"},
                tags=(),
                submitted_at=datetime.now(timezone.utc),
                started_at=None,
                ended_at=None,
                end_reason=None,
                trace_id=None,
                backend="inprocess",
                runtime_id=None,
                owner="alice",
                description=None,
                frames=(),
            )
        )

        async def _get_mock() -> AsyncMock:
            return mock_manager

        app.dependency_overrides[get_manager] = _get_mock

        transport = ASGITransport(app=app)
        client_ctx = AsyncClient(transport=transport, base_url="http://test")
        lifespan_ctx = app.router.lifespan_context(app)
        await lifespan_ctx.__aenter__()
        client = await client_ctx.__aenter__()
        state.append((client_ctx, lifespan_ctx, mock_manager))
        return client, mock_manager

    yield _make

    for client_ctx, lifespan_ctx, _ in state:
        await client_ctx.__aexit__(None, None, None)
        await lifespan_ctx.__aexit__(None, None, None)


# ── TB1 — unknown body key rejected + manager NOT called ───────────────


@pytest.mark.asyncio
async def test_unknown_body_key_rejected_400(app_with_mock_manager):
    """TB1 — Santa-v2 regression net.

    LD_PRELOAD in body credentials must 400 BEFORE the manager is
    invoked. The defence-in-depth requirement: even if some future
    refactor regresses strict-mode logic, the wildcard channel
    stays closed at the schema-equivalent boundary.
    """
    client, mock_manager = await app_with_mock_manager(
        require_per_user_credentials=False
    )
    r = await client.post(
        "/api/v1/sessions",
        headers={"X-API-Key": "alice-key"},
        json={
            "spec": {
                "prompt": "test",
                "cwd": "/tmp",
                "plugins": {"profile": "minimal"},
                "executor": "inprocess",
                "timeout_s": 5,
            },
            "credentials": {"LD_PRELOAD": "/bad.so"},
        },
    )
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["detail"]["code"] == "unsupported_credential_key"
    assert body["detail"]["rejected_keys"] == ["LD_PRELOAD"]
    # The behavioural regression net: manager must NOT have been
    # called. Any future refactor that pushes validation INTO the
    # manager (instead of in front of it) would break this assertion.
    mock_manager.submit.assert_not_called()


# ── TB2 — defence-in-depth regardless of strict mode ───────────────────


@pytest.mark.asyncio
async def test_unknown_body_key_rejected_even_when_strict_off(
    app_with_mock_manager,
):
    """TB2 — even with strict mode OFF, the body allowlist is
    enforced. The security property is INDEPENDENT of the operator
    opt-in flag — closes the channel for single-tenant deployments
    too."""
    client, mock_manager = await app_with_mock_manager(
        require_per_user_credentials=False
    )
    r = await client.post(
        "/api/v1/sessions",
        headers={"X-API-Key": "alice-key"},
        json={
            "spec": {
                "prompt": "test",
                "cwd": "/tmp",
                "plugins": {"profile": "minimal"},
                "executor": "inprocess",
                "timeout_s": 5,
            },
            "credentials": {"PATH": "/attacker/bin"},
        },
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "unsupported_credential_key"
    mock_manager.submit.assert_not_called()


# ── TB3 — allowed body key passes through ──────────────────────────────


@pytest.mark.asyncio
async def test_allowed_body_key_accepted(app_with_mock_manager):
    """TB3 — happy path: ANTHROPIC_API_KEY reaches manager.submit
    intact via the runtime context."""
    client, mock_manager = await app_with_mock_manager(
        require_per_user_credentials=False
    )
    r = await client.post(
        "/api/v1/sessions",
        headers={"X-API-Key": "alice-key"},
        json={
            "spec": {
                "prompt": "test",
                "cwd": "/tmp",
                "plugins": {"profile": "minimal"},
                "executor": "inprocess",
                "timeout_s": 5,
            },
            "credentials": {"ANTHROPIC_API_KEY": "sk-test"},
        },
    )
    # The mock manager returns "mock-sid"; the live response handler
    # then queries detail which won't exist — accept 202 OR 500 from
    # the post-submit lookup. We only care: did submit see the kwarg?
    assert mock_manager.submit.call_count == 1
    call = mock_manager.submit.call_args
    runtime_ctx = call.kwargs["runtime_ctx"]
    assert runtime_ctx.credentials.get("ANTHROPIC_API_KEY") == "sk-test"


# ── TB4 — mixed-keys all-or-nothing rejection ──────────────────────────


@pytest.mark.asyncio
async def test_mixed_keys_one_bad_rejects_all(app_with_mock_manager):
    """TB4 — body with one valid + one invalid key must reject the
    whole submission. The valid key must NEVER be forwarded to the
    manager — otherwise the wildcard channel is half-open."""
    client, mock_manager = await app_with_mock_manager(
        require_per_user_credentials=False
    )
    r = await client.post(
        "/api/v1/sessions",
        headers={"X-API-Key": "alice-key"},
        json={
            "spec": {
                "prompt": "test",
                "cwd": "/tmp",
                "plugins": {"profile": "minimal"},
                "executor": "inprocess",
                "timeout_s": 5,
            },
            "credentials": {
                "ANTHROPIC_API_KEY": "sk-good",
                "LD_PRELOAD": "/bad.so",
            },
        },
    )
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["detail"]["code"] == "unsupported_credential_key"
    assert "LD_PRELOAD" in body["detail"]["rejected_keys"]
    # The valid key must NOT have leaked through.
    mock_manager.submit.assert_not_called()
