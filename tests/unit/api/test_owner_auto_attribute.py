"""Plan 7 Task 6b / D7.26 — sessions router owner resolution.

The router collapses three signals into the final ``owner`` value
that flows down to :meth:`SessionManager.submit`:

  1. ``req.owner`` — operator override in the request body.
  2. ``request.state.api_key_label`` — auto-attributed by
     :class:`APIKeyAuthMiddleware` on a successful auth.
  3. ``"anon"`` — fallback for un-authed test paths
     (``allow_no_keys=True``) and the rare case where the middleware
     didn't run.

These unit tests exercise the precedence purely against the router's
``submit_session`` coroutine — no DB, no executor, no full ASGI stack.
The full e2e attribution behaviour lives in
``tests/integration/test_session_owner_e2e.py``.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gg_relay.api.routers.sessions import submit_session
from gg_relay.api.schemas import (
    PluginManifestIn,
    SessionSpecIn,
    SessionSubmitRequest,
)
from gg_relay.core import SessionState
from gg_relay.session.manager import SessionDetail


def _make_body(owner: str | None = None) -> SessionSubmitRequest:
    return SessionSubmitRequest(
        spec=SessionSpecIn(
            prompt="hi",
            cwd="/tmp",
            plugins=PluginManifestIn(profile="minimal"),
            executor="inprocess",
            timeout_s=5,
            tags=[],
        ),
        credentials={},
        trace_id=None,
        owner=owner,
        description=None,
    )


def _make_request(api_key_label: str | None) -> Any:
    """Build a Starlette-like request stub with the ``state`` shim
    that the router reads via ``getattr(request.state, ...)``."""
    state = MagicMock()
    if api_key_label is None:
        # ``getattr(request.state, "api_key_label", None)`` must
        # observe ``None`` — set the attribute explicitly so the
        # MagicMock doesn't conjure an auto-attribute on access.
        del state.api_key_label
    else:
        state.api_key_label = api_key_label
    request = MagicMock()
    request.state = state
    return request


def _make_manager() -> Any:
    """Manager stub whose ``submit`` returns a fixed sid and whose
    ``get`` returns a SessionDetail whose ``owner`` mirrors what was
    passed to ``submit`` (so the router's response captures it)."""
    from datetime import UTC, datetime

    captured: dict[str, Any] = {}

    async def _submit(spec, **kwargs):
        captured["owner"] = kwargs.get("owner")
        captured["description"] = kwargs.get("description")
        return "sid-1"

    async def _get(sid, **kwargs):
        return SessionDetail(
            id=sid,
            status=SessionState.QUEUED,
            spec_json={},
            tags=(),
            submitted_at=datetime.now(UTC),
            started_at=None,
            ended_at=None,
            end_reason=None,
            trace_id=None,
            backend="inprocess",
            runtime_id=None,
            owner=captured.get("owner"),
            description=captured.get("description"),
        )

    manager = MagicMock()
    manager.submit = AsyncMock(side_effect=_submit)
    manager.get = AsyncMock(side_effect=_get)
    manager._captured = captured  # type: ignore[attr-defined]
    return manager


@pytest.mark.asyncio
async def test_router_uses_req_owner_first() -> None:
    """Operator passes ``owner="bob"`` in the body → wins regardless of
    the auto-attributed label."""
    body = _make_body(owner="bob")
    request = _make_request(api_key_label="alice")
    manager = _make_manager()
    await submit_session(
        request=request,
        body=body,
        manager=manager,
        api_key_id="apikey-abc",
    )
    assert manager._captured["owner"] == "bob"


@pytest.mark.asyncio
async def test_router_falls_back_to_api_key_label() -> None:
    """No ``body.owner`` → auto-attributed ``api_key_label`` flows
    through to ``manager.submit``."""
    body = _make_body(owner=None)
    request = _make_request(api_key_label="alice")
    manager = _make_manager()
    await submit_session(
        request=request,
        body=body,
        manager=manager,
        api_key_id="apikey-abc",
    )
    assert manager._captured["owner"] == "alice"


@pytest.mark.asyncio
async def test_router_falls_back_to_anon_when_no_label() -> None:
    """No body.owner AND no api_key_label (e.g. ``allow_no_keys=True``
    test path) → defaults to the ``"anon"`` literal so the column is
    never written as NULL when the auto-attribute path runs."""
    body = _make_body(owner=None)
    request = _make_request(api_key_label=None)
    manager = _make_manager()
    await submit_session(
        request=request,
        body=body,
        manager=manager,
        api_key_id=None,
    )
    assert manager._captured["owner"] == "anon"
