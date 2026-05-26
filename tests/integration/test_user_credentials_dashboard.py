"""Dashboard credentials pages — Plan v3 §B.7.

End-to-end smoke tests against ``/dashboard/me/credentials`` and
``/dashboard/admin/credentials``. The actual mutations are HTMX
calls into the API routes already covered by
``test_user_credentials_api.py``; this file pins:

  * RBAC at the page boundary (viewer 403, submitter 403 on admin),
  * the feature-disabled banner renders without crashing,
  * the sidebar surfaces the new entries for each role,
  * existing dashboard pages still load (no template regression).

Auth: dashboard pages require a session cookie (X-API-Key alone
triggers the 303 → /dashboard/login redirect). We log in via
``POST /dashboard/login`` with username/password configured by
``dashboard_users_raw`` (or the legacy ``dashboard_admin_password``
for admin). Mirrors the pattern in
``test_dashboard_legacy_admin_role.py``.
"""
from __future__ import annotations

from pathlib import Path

import bcrypt
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.store import create_all_tables, make_async_engine


def _bcrypt(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _make_cfg(
    tmp_path: Path,
    *,
    encryption_key: str | None,
) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/creds-dash.db"
    # Three users via dashboard_users_raw (bcrypt-only) + a parallel
    # role_mapping_raw (the labels are prefixed ``dashboard-`` by the
    # lifespan when minting the internal api_keys row).
    cfg.dashboard_users_raw = (
        f"admin={_bcrypt('admin-pw')},"
        f"alice={_bcrypt('alice-pw')},"
        f"viewer-bot={_bcrypt('viewer-pw')}"
    )
    cfg.api_keys_raw = "k1"
    cfg.role_mapping_raw = (
        "dashboard-admin=admin,"
        "dashboard-alice=submitter,"
        "dashboard-viewer-bot=viewer"
    )
    cfg.dashboard_session_secret = SecretStr(
        "dash-creds-test-secret-32-bytes-min"
    )
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://t"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    # Hermetic: ALWAYS overwrite this field — even with None — so the
    # test doesn't accidentally inherit a value pydantic-settings auto-
    # loaded from the developer's repo-root ``.env``. The previous
    # ``if not None`` form treated ``None`` as "leave whatever was
    # auto-loaded", which silently disabled the "Feature disabled"
    # banner branch on machines with an encryption key in ``.env``.
    cfg.credentials_encryption_key = (
        SecretStr(encryption_key) if encryption_key is not None else None
    )
    return cfg


@pytest_asyncio.fixture
async def app_for_encryption_key(tmp_path: Path):
    """Returns an async factory: ``await make(encryption_key=...)`` →
    ``(client, cfg)``. Each call yields a fresh app + fresh session."""
    contexts: list = []

    async def _make(encryption_key: str | None):
        cfg = _make_cfg(tmp_path, encryption_key=encryption_key)
        eng = make_async_engine(cfg.database_url)
        await create_all_tables(eng)
        await eng.dispose()
        app = create_app(cfg)
        transport = ASGITransport(app=app)
        ctx_client = AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = await ctx_client.__aenter__()
        contexts.append((ctx_client, lifespan))
        return client

    yield _make

    for ctx_client, lifespan in contexts:
        await ctx_client.__aexit__(None, None, None)
        await lifespan.__aexit__(None, None, None)


async def _login(client: AsyncClient, username: str, password: str) -> None:
    r = await client.post(
        "/dashboard/login",
        data={"username": username, "password": password},
    )
    assert r.status_code == 303, r.text


pytestmark = pytest.mark.asyncio


async def test_me_credentials_page_renders_for_submitter(
    app_for_encryption_key,
):
    """Alice (submitter) gets a 200 with the credentials form."""
    key = Fernet.generate_key().decode("utf-8")
    client: AsyncClient = await app_for_encryption_key(encryption_key=key)
    await _login(client, "alice", "alice-pw")
    r = await client.get("/dashboard/me/credentials")
    assert r.status_code == 200, r.text
    assert "My upstream credentials" in r.text
    assert "ANTHROPIC_API_KEY" in r.text


async def test_me_credentials_page_blocks_viewer(app_for_encryption_key):
    """Viewer gets 403 — storing creds you can't use is meaningless."""
    key = Fernet.generate_key().decode("utf-8")
    client: AsyncClient = await app_for_encryption_key(encryption_key=key)
    await _login(client, "viewer-bot", "viewer-pw")
    r = await client.get("/dashboard/me/credentials")
    assert r.status_code == 403, r.text


async def test_admin_credentials_page_blocks_submitter(
    app_for_encryption_key,
):
    """Alice (submitter, not admin) gets 403 on the admin override page."""
    key = Fernet.generate_key().decode("utf-8")
    client: AsyncClient = await app_for_encryption_key(encryption_key=key)
    await _login(client, "alice", "alice-pw")
    r = await client.get("/dashboard/admin/credentials")
    assert r.status_code == 403, r.text


async def test_admin_credentials_page_renders_for_admin(
    app_for_encryption_key,
):
    """Admin gets a 200 with the full per-user table."""
    key = Fernet.generate_key().decode("utf-8")
    client: AsyncClient = await app_for_encryption_key(encryption_key=key)
    await _login(client, "admin", "admin-pw")
    r = await client.get("/dashboard/admin/credentials")
    assert r.status_code == 200, r.text
    assert "Admin · per-user credentials" in r.text
    assert "AWS_ACCESS_KEY_ID" in r.text


async def test_feature_disabled_banner_renders_without_crash(
    app_for_encryption_key,
):
    """When the encryption key is missing the page still renders 200
    with a warning banner instead of 5xx-ing the user."""
    client: AsyncClient = await app_for_encryption_key(encryption_key=None)
    await _login(client, "alice", "alice-pw")
    r = await client.get("/dashboard/me/credentials")
    assert r.status_code == 200, r.text
    assert "Feature disabled" in r.text
    assert "RELAY_CREDENTIALS_ENCRYPTION_KEY" in r.text


async def test_sidebar_shows_my_credentials_for_submitter(
    app_for_encryption_key,
):
    """[Plan v3 §B.7 regression net] alice (submitter) sees a clickable
    "My credentials" entry in the left sidebar from EVERY page.

    The Cmd+K palette also surfaces it, but operators who don't know
    about the palette must still discover the feature from the
    sidebar. A previous iteration of §B.7 only added the entry to
    ``_cmdk_pages`` (palette) and missed ``_sidebar.html`` (the
    visible nav). This test would have failed that iteration.
    """
    key = Fernet.generate_key().decode("utf-8")
    client: AsyncClient = await app_for_encryption_key(encryption_key=key)
    await _login(client, "alice", "alice-pw")
    # Check from a couple of distinct pages so a template that fails
    # to include the sidebar partial also surfaces here.
    for path in ("/dashboard/overview", "/dashboard/kanban"):
        r = await client.get(path)
        assert r.status_code == 200, f"{path}: {r.text[:200]}"
        assert 'href="/dashboard/me/credentials"' in r.text, (
            f"sidebar missing 'My credentials' entry on {path} for "
            f"submitter alice; current text: {r.text[:500]}"
        )


async def test_sidebar_shows_admin_credentials_for_admin(
    app_for_encryption_key,
):
    """[Plan v3 §B.7 regression net] admin sees BOTH 'My credentials'
    AND admin-only 'Credentials' in the sidebar.

    Admin is also a submitter+ so the /me entry must still appear;
    the admin-only entry is in addition to it, not instead of it."""
    key = Fernet.generate_key().decode("utf-8")
    client: AsyncClient = await app_for_encryption_key(encryption_key=key)
    await _login(client, "admin", "admin-pw")
    r = await client.get("/dashboard/overview")
    assert r.status_code == 200, r.text
    assert 'href="/dashboard/me/credentials"' in r.text
    assert 'href="/dashboard/admin/credentials"' in r.text


async def test_sidebar_hides_credentials_for_viewer(
    app_for_encryption_key,
):
    """[Plan v3 §B.7 regression net] viewer sees NEITHER credentials
    entry — surfacing a link that always 403s would be a UX foot-gun
    (better to not show it at all than show a disabled affordance)."""
    key = Fernet.generate_key().decode("utf-8")
    client: AsyncClient = await app_for_encryption_key(encryption_key=key)
    await _login(client, "viewer-bot", "viewer-pw")
    r = await client.get("/dashboard/overview")
    assert r.status_code == 200, r.text
    assert 'href="/dashboard/me/credentials"' not in r.text
    assert 'href="/dashboard/admin/credentials"' not in r.text


async def test_existing_dashboard_pages_still_load(app_for_encryption_key):
    """Defensive regression net: adding the two new sidebar entries
    must not break existing pages (a typo in the helper or a
    template would surface here)."""
    key = Fernet.generate_key().decode("utf-8")
    client: AsyncClient = await app_for_encryption_key(encryption_key=key)
    await _login(client, "admin", "admin-pw")
    for path in [
        "/dashboard/overview",
        "/dashboard/kanban",
        "/dashboard/list",
        "/dashboard/admin/keys",
    ]:
        r = await client.get(path)
        assert r.status_code == 200, f"{path}: {r.text[:200]}"


# ── Plan v5 §3.4 — strict-mode dashboard integration ───────────────────


async def test_new_session_template_contains_missing_creds_handler():
    """TD1a — Plan v5 / Santa-v3 reviewer M.S3 regression net.

    Greps for the EXACT JS branch substring rather than the two
    words independently. If a refactor changes the JS handler or
    template path, this surfaces the divergence immediately —
    keeping the two ends of the dashboard ⇄ API contract in sync.
    """
    template_path = (
        Path(__file__).resolve().parents[2]
        / "src/gg_relay/dashboard/templates/new.html"
    )
    assert template_path.exists(), f"template missing at {template_path}"
    body = template_path.read_text()
    # The pinned JS branch substring — not just two independent words.
    assert "code === 'missing_credentials'" in body, (
        "new.html must contain the JS branch that handles the 400 "
        "missing_credentials response from POST /api/v1/sessions"
    )
    assert "/dashboard/me/credentials" in body, (
        "new.html must contain the anchor target for the structured "
        "error banner so users can self-serve"
    )


async def test_api_returns_missing_credentials_code_under_strict(
    tmp_path: Path,
):
    """TD1b — End-to-end contract: with strict mode on, a dashboard
    cookie session POST to /api/v1/sessions without creds returns
    ``detail.code == "missing_credentials"``. This is the JSON shape
    the new.html JS branch (TD1a) parses to render the structured
    error banner. Together TD1a + TD1b pin both ends of the
    dashboard ⇄ API contract without needing a browser.
    """
    key = Fernet.generate_key().decode("utf-8")
    cfg = _make_cfg(tmp_path, encryption_key=key)
    cfg.require_per_user_credentials = True   # v5 strict mode on
    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    app = create_app(cfg)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        try:
            # Authenticate as alice (submitter, non-admin) so strict
            # mode applies. Dashboard cookie auth.
            await _login(client, "alice", "alice-pw")
            r = await client.post(
                "/api/v1/sessions",
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
            assert r.status_code == 400, r.text
            body = r.json()
            assert body["detail"]["code"] == "missing_credentials", (
                "API contract must return detail.code='missing_credentials' "
                "so the dashboard JS branch (TD1a) can render the "
                "structured error UI"
            )
            # Sanity-check the actionable message points to the dashboard.
            assert "/dashboard/me/credentials" in body["detail"]["message"]
        finally:
            await lifespan.__aexit__(None, None, None)
