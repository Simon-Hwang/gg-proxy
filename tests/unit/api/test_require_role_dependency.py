"""Unit tests for the ``require_role`` dependency (Plan 8 D8.22 / Task 4).

Six focused tests covering:

* viewer can't perform a submitter action (403 ``insufficient_role``)
* submitter can perform a submitter action (200)
* admin can perform any action (200 across the hierarchy)
* unknown label falls back to ``viewer`` (403)
* own-session exception lets a submitter cancel their own session
* own-session exception still refuses on someone else's session

The tests construct mock requests directly (``MagicMock``) rather
than going through a FastAPI TestClient — that's deliberate, the
goal is to assert the pure authz logic without the framework noise.
The end-to-end tests in ``tests/integration/test_role_endpoint_e2e.py``
cover the FastAPI wiring.

NOTE: the root ``tests/conftest.py`` autouse fixture patches
``_resolve_role`` to grant ``admin`` when ``cfg.role_mapping`` is
empty (test-mode convenience). Every test below sets a non-empty
role_mapping on the mock cfg, so the patch's empty-mapping branch
never kicks in — the real ``_resolve_role`` logic runs end-to-end.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


def _mock_request(
    label: str | None,
    role_mapping: dict[str, str],
    store: Any = None,
) -> MagicMock:
    """Build a MagicMock that walks like a Starlette Request enough for the dep.

    ``role_mapping`` MUST be a real dict (not a MagicMock) so the
    conftest patch's ``if not role_map`` check returns ``False`` and
    the patched resolver delegates to the production logic — that's
    the path we're actually testing.
    """
    request = MagicMock()
    request.state.api_key_label = label
    # Replace MagicMock's auto-spawned attrs with concrete values so
    # the dependency sees a real dict (not a truthy MagicMock).
    cfg = MagicMock()
    cfg.role_mapping = role_mapping
    request.app.state.config = cfg
    request.app.state.store = store
    return request


# ── require_role ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_viewer_cannot_post_submitter_required() -> None:
    from gg_relay.api.dependencies.require_role import require_role

    dep = require_role("submitter")
    request = _mock_request("alice", {"alice": "viewer"})

    with pytest.raises(HTTPException) as exc_info:
        await dep(request)

    assert exc_info.value.status_code == 403
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["error"] == "forbidden"
    assert detail["code"] == "insufficient_role"
    assert detail["required_role"] == "submitter"
    assert detail["current_role"] == "viewer"


@pytest.mark.asyncio
async def test_submitter_can_post_submitter_action() -> None:
    from gg_relay.api.dependencies.require_role import require_role

    dep = require_role("submitter")
    request = _mock_request("alice", {"alice": "submitter"})

    role = await dep(request)
    assert role == "submitter"
    assert request.state.role == "submitter"


@pytest.mark.asyncio
async def test_admin_can_perform_any_action() -> None:
    from gg_relay.api.dependencies.require_role import require_role

    dep_submitter = require_role("submitter")
    dep_admin = require_role("admin")
    request = _mock_request("alice", {"alice": "admin"})

    assert await dep_submitter(request) == "admin"
    assert await dep_admin(request) == "admin"


@pytest.mark.asyncio
async def test_unknown_label_defaults_to_viewer() -> None:
    """An authenticated label that's not in role_mapping should
    fall back to ``viewer`` (least privilege), NOT inherit the
    mapping default of some random other key."""
    from gg_relay.api.dependencies.require_role import require_role

    dep = require_role("submitter")
    # role_mapping is non-empty so the conftest patch delegates to
    # the real resolver; "unknown_user" is absent from the map.
    request = _mock_request("unknown_user", {"alice": "admin"})

    with pytest.raises(HTTPException) as exc_info:
        await dep(request)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["current_role"] == "viewer"


# ── require_role_or_own_session ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_own_session_exception_submitter_can_cancel_own() -> None:
    """A submitter (below the ``admin`` threshold) can still
    cancel their *own* session because of the own-session
    fallback."""
    from gg_relay.api.dependencies.require_role import (
        require_role_or_own_session,
    )

    async def mock_get_session(sid: str) -> dict[str, Any]:
        return {"id": sid, "owner": "alice"}

    store = MagicMock()
    store.get_session = mock_get_session

    dep = require_role_or_own_session("admin")
    request = _mock_request("alice", {"alice": "submitter"}, store=store)

    role = await dep(request, "sid-1")
    # Returns ``min_role`` to signal that the policy was satisfied
    # via the own-session fallback (not via role elevation).
    assert role == "admin"
    assert request.state.role == "admin"


@pytest.mark.asyncio
async def test_submitter_cannot_cancel_others_session() -> None:
    """A submitter trying to cancel someone else's session must
    get a 403 ``not_owner`` body that surfaces the actual owner so
    the dashboard can render an actionable error."""
    from gg_relay.api.dependencies.require_role import (
        require_role_or_own_session,
    )

    async def mock_get_session(sid: str) -> dict[str, Any]:
        return {"id": sid, "owner": "bob"}

    store = MagicMock()
    store.get_session = mock_get_session

    dep = require_role_or_own_session("admin")
    request = _mock_request("alice", {"alice": "submitter"}, store=store)

    with pytest.raises(HTTPException) as exc_info:
        await dep(request, "sid-2")

    assert exc_info.value.status_code == 403
    detail = exc_info.value.detail
    assert detail["code"] == "not_owner"
    assert detail["required_role"] == "admin"
    assert detail["current_role"] == "submitter"
    assert detail["session_owner"] == "bob"
