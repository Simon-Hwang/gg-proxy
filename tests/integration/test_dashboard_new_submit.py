"""Plan 8 Task 16 / D8.14 — dashboard new-session form integration tests.

Four scenarios cover the form's observable contract:

* ``test_new_form_renders_with_template_options`` — the template
  ``<select>`` lists the caller's own + every shared template; a
  private template owned by another user is hidden.
* ``test_new_form_preload_via_template_url`` — ``?template=<id>``
  on a template the caller can read pre-fills prompt + description
  into the form and surfaces the "Using template …" info banner.
* ``test_new_form_preload_private_other_template_warns`` — pointing
  at another user's *private* template surfaces an inline
  ``alert-warning`` with "is private" wording instead of leaking
  the body.
* ``test_new_form_duplicate_warning_endpoint`` — the
  ``/dashboard/new/check-duplicate`` HTMX fragment renders a
  warning panel when a same-owner same-prompt session sits in the
  last 10 minutes; an absent / off-window prompt yields an empty
  body so HTMX clears any earlier warning.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import bcrypt
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.store import SqlAlchemyStore, create_all_tables, make_async_engine


def _bcrypt_hash(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")


def _make_cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/new-form.db"
    cfg.api_keys_raw = ""
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.dashboard_session_secret = SecretStr(
        "test-secret-32-bytes-or-longer-xxxxxxxx"
    )
    cfg.dashboard_users_raw = f"alice={_bcrypt_hash('alice-pw')}"
    cfg.public_base_url = "http://t"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def client_and_store(tmp_path: Path) -> Any:
    cfg = _make_cfg(tmp_path)
    app = create_app(cfg)

    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac, app.router.lifespan_context(app):
        r = await ac.post(
            "/dashboard/login",
            data={"username": "alice", "password": "alice-pw"},
        )
        assert r.status_code == 303, r.text
        yield ac, app.state.store


async def test_new_form_renders_with_template_options(
    client_and_store,
) -> None:
    """The template ``<select>`` lists the caller's own + every shared
    template; a private template owned by another user is hidden."""
    client, store = client_and_store
    assert isinstance(store, SqlAlchemyStore)

    await store.create_template(
        name="alice-own-tpl",
        creator="dashboard-alice",
        prompt="alice's saved prompt body",
        description="alice's note",
        shared=False,
    )
    await store.create_template(
        name="bob-shared-tpl",
        creator="bob",
        prompt="team scratchpad prompt",
        shared=True,
    )
    await store.create_template(
        name="bob-private-tpl",
        creator="bob",
        prompt="bob's secret prompt",
        shared=False,
    )

    r = await client.get("/dashboard/new")
    assert r.status_code == 200, r.text
    body = r.text
    assert "Submit new session" in body
    assert "alice-own-tpl" in body
    assert "bob-shared-tpl" in body
    assert "bob-private-tpl" not in body
    # Form posts to the API submit endpoint via the cookie middleware.
    assert 'hx-post="/api/v1/sessions"' in body


async def test_new_form_preload_via_template_url(client_and_store) -> None:
    """``?template=<id>`` pre-fills prompt + description and shows the
    "Using template" info banner."""
    client, store = client_and_store
    assert isinstance(store, SqlAlchemyStore)

    t = await store.create_template(
        name="preload-test",
        creator="dashboard-alice",
        prompt="preload prompt body for the form",
        description="preload description text",
        shared=False,
    )
    tid = int(t["id"])

    r = await client.get(f"/dashboard/new?template={tid}")
    assert r.status_code == 200, r.text
    body = r.text
    assert "preload prompt body for the form" in body
    assert "preload description text" in body
    assert "Using template" in body
    assert "preload-test" in body


async def test_new_form_preload_private_other_template_warns(
    client_and_store,
) -> None:
    """Pointing ``?template=<id>`` at another user's private template
    surfaces an inline ``alert-warning`` ("is private") and does NOT
    leak the body into the form."""
    client, store = client_and_store
    assert isinstance(store, SqlAlchemyStore)

    t = await store.create_template(
        name="bob-secret",
        creator="bob",
        prompt="bob's private prompt body that must not leak",
        description="bob's private note",
        shared=False,
    )
    tid = int(t["id"])

    r = await client.get(f"/dashboard/new?template={tid}")
    assert r.status_code == 200, r.text
    body = r.text
    assert "is private" in body
    assert "alert-warning" in body
    # The body of the private template MUST NOT be rendered into the
    # form — verify the prompt text + the description are absent.
    assert "must not leak" not in body
    assert "bob's private note" not in body


async def test_new_form_duplicate_warning_endpoint(client_and_store) -> None:
    """The HTMX duplicate-warning fragment renders a panel when a
    same-owner session with the same prompt prefix sits within the
    last 10 minutes; a fresh / unrelated prompt yields empty body."""
    client, store = client_and_store
    assert isinstance(store, SqlAlchemyStore)

    now = datetime.now(UTC)
    await store.create_session(
        id="dup-001-aaaa-bbbb-cccc-dddddddddddd",
        spec_json={"prompt": "rerun the smoke suite for me"},
        trace_id=None,
        backend="inprocess",
        owner="dashboard-alice",
        submitted_at=now - timedelta(minutes=2),
    )

    r = await client.get(
        "/dashboard/new/check-duplicate",
        params={"prompt": "rerun the smoke suite for me"},
    )
    assert r.status_code == 200, r.text
    body = r.text
    assert "similar prompt recently" in body
    # Renders the short-id link to the duplicate session.
    assert "dup-001-" in body

    # An unrelated prompt produces an empty fragment (HTMX clears the
    # warning slot).
    r2 = await client.get(
        "/dashboard/new/check-duplicate",
        params={"prompt": "totally unrelated prompt nobody used"},
    )
    assert r2.status_code == 200
    assert r2.text.strip() == ""

    # Below the 5-character minimum the endpoint short-circuits to
    # empty without consulting the store.
    r3 = await client.get(
        "/dashboard/new/check-duplicate", params={"prompt": "rer"}
    )
    assert r3.status_code == 200
    assert r3.text.strip() == ""
