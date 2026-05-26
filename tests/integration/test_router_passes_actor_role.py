# ruff: noqa: E501 — table comments intentionally over 100 chars for grep-ability.
"""Router-passes-actor_role behavioural net — Plan v5 §3.3.

Replaces the v1 fragile Make-target grep audit with a real
dependency-overridden FastAPI client. Asserts that the route DOES
forward ``actor_role=`` resolved from ``_rr_mod._resolve_role(request)``
into ``manager.submit`` / ``manager.retry``.

Why this matters: if any future refactor stops passing ``actor_role``
to the manager, strict-mode enforcement silently regresses to
"non-admin treated as None → still rejected" — but the WARN
message would be wrong AND any future "admin escape hatch" code
that reads ``actor_role`` would mis-identify legitimate admins. The
behavioural test mocks the manager so it sees exactly what arrives.

Test coverage map (Plan v5 §3.3):

| ID  | Test                                                             | What it pins |
|-----|------------------------------------------------------------------|---|
| TR1 | test_submit_route_passes_actor_role_kwarg                        | POST /api/v1/sessions → manager.submit has actor_role |
| TR2 | test_batch_retry_passes_actor_role_kwarg                         | POST /api/v1/sessions/batch retry → manager.retry has actor_role |
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
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
from gg_relay.core import SessionState
from gg_relay.session.manager import SessionDetail
from gg_relay.store import create_all_tables, make_async_engine


def _make_cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/actor-role.db"
    cfg.api_keys_raw = "alice-key:alice,admin-key:admin"
    cfg.role_mapping_raw = "alice=submitter,admin=admin"
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://localhost:8000"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    from pydantic import SecretStr
    cfg.credentials_encryption_key = SecretStr(
        Fernet.generate_key().decode("utf-8")
    )
    return cfg


def _mock_session_detail() -> SessionDetail:
    return SessionDetail(
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


@pytest_asyncio.fixture
async def app_with_mock(
    tmp_path: Path,
) -> AsyncIterator[Callable[..., Any]]:
    state: list[Any] = []

    async def _make() -> tuple[AsyncClient, AsyncMock]:
        cfg = _make_cfg(tmp_path)
        eng = make_async_engine(cfg.database_url)
        await create_all_tables(eng)
        await eng.dispose()
        app = create_app(cfg)

        mock_manager = AsyncMock()
        mock_manager.submit = AsyncMock(return_value="mock-sid")
        mock_manager.retry = AsyncMock(return_value="new-mock-sid")
        mock_manager.get = AsyncMock(return_value=_mock_session_detail())

        async def _get_mock() -> AsyncMock:
            return mock_manager

        app.dependency_overrides[get_manager] = _get_mock
        transport = ASGITransport(app=app)
        client_ctx = AsyncClient(transport=transport, base_url="http://test")
        lifespan_ctx = app.router.lifespan_context(app)
        await lifespan_ctx.__aenter__()
        # After lifespan boots, replace app.state.store with a mock
        # so batch_sessions' get_session(sid) returns a session-like
        # object rather than None — otherwise the retry branch never
        # executes (TR2 needs to reach the manager.retry call).
        mock_store = AsyncMock()
        mock_store.get_session = AsyncMock(
            return_value={"owner": "admin", "id": "existing-sid-1"}
        )
        app.state.store = mock_store
        client = await client_ctx.__aenter__()
        state.append((client_ctx, lifespan_ctx))
        return client, mock_manager

    yield _make
    for client_ctx, lifespan_ctx in state:
        await client_ctx.__aexit__(None, None, None)
        await lifespan_ctx.__aexit__(None, None, None)


# ── TR1 — submit forwards actor_role ───────────────────────────────────


@pytest.mark.asyncio
async def test_submit_route_passes_actor_role_kwarg(app_with_mock):
    """TR1 — submitter alice posts → manager.submit MUST receive
    ``actor_role='submitter'`` (the value resolved by
    ``_rr_mod._resolve_role`` from the api-key role mapping)."""
    client, mock_manager = await app_with_mock()
    await client.post(
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
            "credentials": {},
        },
    )
    assert mock_manager.submit.call_count == 1
    kwargs = mock_manager.submit.call_args.kwargs
    assert "actor_role" in kwargs, (
        "submit_session must forward actor_role into manager.submit "
        "kwargs — otherwise strict-mode enforcement silently regresses"
    )
    assert kwargs["actor_role"] == "submitter"
    assert kwargs["actor_label"] == "alice"


# ── TR2 — batch retry forwards actor_role ──────────────────────────────


@pytest.mark.asyncio
async def test_batch_retry_passes_actor_role_kwarg(app_with_mock):
    """TR2 — batch retry MUST forward ``actor_role`` into
    ``manager.retry``. Without this the retry path bypasses strict-
    mode enforcement entirely."""
    client, mock_manager = await app_with_mock()
    await client.post(
        "/api/v1/sessions/batch",
        headers={"X-API-Key": "admin-key"},
        json={"ids": ["existing-sid-1"], "action": "retry"},
    )
    assert mock_manager.retry.call_count == 1
    call = mock_manager.retry.call_args
    assert "actor_role" in call.kwargs, (
        "batch_sessions retry branch must forward actor_role into "
        "manager.retry kwargs"
    )
    assert call.kwargs["actor_role"] == "admin"
    assert call.kwargs["actor"] == "admin"
