"""Regression test for the "clicking New Session does nothing" bug.

Pinned contract: when the operator logs in via the legacy
``dashboard_admin_password`` path AND no ``role_mapping_raw`` is
configured, ``_dashboard_role`` must resolve the
``dashboard-admin`` label to the ``admin`` role — not the safe
default ``viewer``.

If the legacy admin lands as ``viewer`` every "+ New session"
affordance renders as a disabled ``<span>`` with no ``href``,
so clicking it does nothing. This was the user-reported bug.

The two tests below cover:

* the sidebar — the global CTA must be a real
  ``<a href="/dashboard/new" ...>``, not the disabled span variant.
* the overview page header — same CTA contract.

We keep the assertions structural (href + class) rather than
text-based so future microcopy edits don't accidentally pass the
test while the link is still broken.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.store import create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


def _cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/legacy-admin.db"
    cfg.api_keys_raw = "k1"
    # Deliberately NOT setting role_mapping_raw / dashboard_users_raw
    # — this is the "default install" shape that triggered the bug.
    cfg.dashboard_admin_password = SecretStr("hunter2")
    cfg.dashboard_session_secret = SecretStr(
        "legacy-admin-test-secret-32-bytes-min"
    )
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://t"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def client(tmp_path: Path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac, app.router.lifespan_context(app):
        yield ac


async def _login_legacy_admin(ac: AsyncClient) -> None:
    r = await ac.post(
        "/dashboard/login",
        data={"username": "admin", "password": "hunter2"},
    )
    assert r.status_code == 303, r.text


async def test_legacy_admin_sees_enabled_new_session_in_sidebar(
    client: AsyncClient,
) -> None:
    """Sidebar global CTA must render as a real ``<a>`` link.

    The disabled variant is a ``<span class="cta-primary cta-disabled"
    aria-disabled="true">`` with no href; clicking does nothing.
    If the role detection regresses to ``viewer`` for the legacy
    admin, that span renders instead and this assertion catches it.
    """
    await _login_legacy_admin(client)
    r = await client.get("/dashboard/overview")
    assert r.status_code == 200, r.text
    # Must have at least one real link CTA pointing at /dashboard/new
    assert 'href="/dashboard/new" class="cta-primary"' in r.text, (
        "Sidebar 'New session' CTA must be an enabled <a>; "
        "got a disabled <span> instead (legacy admin role regressed to viewer)"
    )
    # And must NOT have rendered the disabled variant for the legacy admin
    assert 'class="cta-primary cta-disabled"' not in r.text, (
        "Legacy admin should not see disabled CTAs"
    )


async def test_legacy_admin_sees_admin_role_badge(
    client: AsyncClient,
) -> None:
    """Sidebar footer should render the ``role-admin`` badge.

    The badge class is a stable RBAC signal — viewer/submitter/admin
    each get a distinct CSS class; we pin the admin one so future
    role-resolution changes that demote the legacy admin are
    surfaced immediately.
    """
    await _login_legacy_admin(client)
    r = await client.get("/dashboard/overview")
    assert r.status_code == 200, r.text
    assert "role-admin" in r.text, (
        "Legacy admin should render the role-admin badge, "
        "not role-viewer or role-submitter"
    )
    assert "role-viewer" not in r.text, (
        "Legacy admin must not be tagged as viewer"
    )


async def test_legacy_admin_can_reach_new_session_form(
    client: AsyncClient,
) -> None:
    """End-to-end: clicking through the CTA actually loads the form.

    A 200 on ``/dashboard/new`` plus the submit button text confirms
    the role gate doesn't reject the legacy admin at the form route
    either.
    """
    await _login_legacy_admin(client)
    r = await client.get("/dashboard/new")
    assert r.status_code == 200, r.text
    # The form's primary CTA — stable text used elsewhere too
    assert "Submit new session" in r.text


# ── Routes that previously bypassed _dashboard_role ──────────────
# Pre-fix these 5 handlers did their own ``role_map.get(label, "viewer")``
# inline lookup and never consulted the legacy-admin fallback. The
# user-visible symptom was "logged in as admin, sidebar shows API keys
# link, clicking it returns 403". Each of these tests pins one of the
# fixed handlers.


async def test_legacy_admin_can_open_api_keys_page(
    client: AsyncClient,
) -> None:
    """``/dashboard/admin/keys`` was the original user-reported bug:
    the sidebar shows the link for admin role, but the page handler
    used a raw role_map lookup and returned 403 for the legacy admin.
    Pin a 200 + a piece of admin-only chrome (the create form)."""
    await _login_legacy_admin(client)
    r = await client.get("/dashboard/admin/keys")
    assert r.status_code == 200, (
        f"legacy admin should see API keys page, got {r.status_code}: "
        f"{r.text[:200]}"
    )
    # Admin-only mutation form must be rendered (the page deliberately
    # does NOT render it for non-admins).
    assert "Forbidden" not in r.text
    # The page also references the underlying API surface.
    assert "/api/v1/admin/keys" in r.text


async def test_legacy_admin_sees_all_templates(
    client: AsyncClient,
) -> None:
    """``/dashboard/templates`` previously demoted legacy admin to
    viewer scope (``is_admin=False`` in ``store.list_templates``)
    so the operator only saw their own templates, not the team's.
    Verify the page loads as admin scope by asserting the response
    is a 200 with the page chrome."""
    await _login_legacy_admin(client)
    r = await client.get("/dashboard/templates")
    assert r.status_code == 200
    assert "Prompt Templates" in r.text


async def test_legacy_admin_can_open_session_audit(
    client: AsyncClient,
) -> None:
    """``/dashboard/sessions/{id}/audit`` returned 403 for legacy
    admin on sessions they did not personally own — the handler
    bypassed _dashboard_role and never knew they were admin."""
    await _login_legacy_admin(client)
    # An audit fragment for a non-existent session should 404
    # (admin role passes the RBAC gate; the not-found message is
    # what we read instead of "Forbidden").
    r = await client.get("/dashboard/sessions/nonexistent-sid/audit")
    assert r.status_code == 404
    assert "Forbidden" not in r.text
    assert "not found" in r.text.lower()


async def test_legacy_admin_can_open_session_comments(
    client: AsyncClient,
) -> None:
    """``/dashboard/sessions/{id}/comments`` was the same bug as
    session_audit — admin couldn't see comments on other operators'
    sessions because they were silently demoted to viewer."""
    await _login_legacy_admin(client)
    r = await client.get("/dashboard/sessions/nonexistent-sid/comments")
    assert r.status_code == 404
    assert "Forbidden" not in r.text


async def test_legacy_admin_new_session_form_shows_admin_chrome(
    client: AsyncClient,
) -> None:
    """``/dashboard/new`` already loads for any logged-in user (POST
    is gated server-side), but the form rendered with ``is_admin=False``
    for the legacy admin, hiding any admin-only template visibility
    hints. Verify the page renders without 403 banner."""
    await _login_legacy_admin(client)
    r = await client.get("/dashboard/new")
    assert r.status_code == 200
    assert "Forbidden" not in r.text


# ── Lifespan internal-key minting for legacy admin ───────────────
# Pre-fix the lifespan only minted dashboard_internal_keys for users
# in ``cfg.dashboard_users`` (the bcrypt multi-user path). Operators
# who only set ``RELAY_DASHBOARD_ADMIN_PASSWORD`` logged in as "admin"
# but had no entry in ``app.state.dashboard_internal_keys``, so the
# DashboardCookieMiddleware never injected ``X-API-Key``. Every
# dashboard → ``/api/v1/*`` mutation died with 401 ``invalid_api_key``.
# These three tests pin the contract end-to-end.


async def test_lifespan_mints_internal_key_for_legacy_admin(
    client: AsyncClient,
) -> None:
    """The lifespan must seed ``dashboard_internal_keys["admin"]``
    when ``dashboard_admin_password`` is set (even with empty
    ``dashboard_users``). Probe ``app.state`` directly so we catch
    the regression even if the proxy injection is later refactored."""
    transport = client._transport  # type: ignore[attr-defined]
    app = transport.app  # type: ignore[attr-defined]
    keys = getattr(app.state, "dashboard_internal_keys", {})
    assert "admin" in keys, (
        "lifespan must mint an internal key for legacy admin when "
        f"dashboard_admin_password is set; got keys={list(keys)!r}"
    )
    assert isinstance(keys["admin"], str) and len(keys["admin"]) > 10


async def test_legacy_admin_api_mutation_does_not_401(
    client: AsyncClient,
) -> None:
    """End-to-end: legacy admin POSTs ``/api/v1/sessions`` from the
    cookie session. Pre-fix this was 401 ``invalid_api_key`` because
    no synthetic header was injected. Post-fix any non-401 response
    is acceptable — a schema-validation 422 from passing an empty
    body is fine; the point is auth passed."""
    await _login_legacy_admin(client)
    r = await client.post("/api/v1/sessions", json={"prompt": "hi"})
    assert r.status_code != 401, (
        f"legacy admin must not get 401 on /api/v1/* — got {r.status_code}: "
        f"{r.text[:200]}"
    )
    assert "invalid_api_key" not in r.text


async def test_legacy_admin_can_self_serve_api_keys_via_web(
    client: AsyncClient,
) -> None:
    """The user-visible win: admin can now create new API keys via
    the web (``/dashboard/admin/keys`` → ``POST /api/v1/admin/keys``)
    without ever touching the CLI. Pre-fix this returned 401 because
    the synthetic header was missing."""
    await _login_legacy_admin(client)
    r = await client.post(
        "/api/v1/admin/keys",
        json={"label": "ci-bot", "role": "submitter"},
    )
    assert r.status_code == 201, (
        f"expected 201 on POST /api/v1/admin/keys, got {r.status_code}: "
        f"{r.text[:200]}"
    )
    body = r.json()
    assert body["label"] == "ci-bot"
    assert body["role"] == "submitter"
    # The endpoint returns the raw_key exactly once for the operator
    # to copy out — confirm the self-service path returns it.
    assert body.get("raw_key", "").startswith("rk_"), body
