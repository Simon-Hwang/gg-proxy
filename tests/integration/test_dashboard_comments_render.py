"""Plan 8 Task 8 / D8.5 — dashboard comments fragment + edit form tests.

Drives ``GET /dashboard/sessions/{sid}/comments`` and
``GET /dashboard/comments/{cid}/edit`` through the live FastAPI app
with a dashboard cookie session attached. Three tests:

* ``test_dashboard_comments_renders_existing`` — alice logs in via
  the bcrypt path. Two comments are seeded directly through the
  store (one by alice, one by bob). The HTMX endpoint returns 200
  and the rendered fragment carries both ``<li class="comment-item">``
  rows + the post-new-comment form.
* ``test_dashboard_comments_empty_session_renders_form`` — alice
  visits a session with no comments. The fragment swaps in the
  ``No comments yet.`` empty state alongside the post form so the
  operator can still submit the first comment.
* ``test_dashboard_comment_edit_form_author_only`` — alice's session
  has a comment authored by ``dashboard-bob``. Alice (a non-author)
  hits ``/dashboard/comments/{cid}/edit`` and gets a 403 ``Forbidden.``
  HTMX fragment. We then re-seed a comment authored by
  ``dashboard-alice`` and confirm she can fetch the edit form (200 +
  textarea pre-filled with the original markdown).

Comments are seeded directly through the store — the test intent is
the render + author-check contract, not the upstream comments API
plumbing (which Task 7's tests already cover).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import bcrypt
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.executor.protocol import ExecutorBackend
from gg_relay.session.frames import make_msg_chunk, make_session_end
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.spec import SessionSpec
from gg_relay.session.transport.protocol import SessionTransport
from gg_relay.store import SqlAlchemyStore, create_all_tables, make_async_engine


async def _trivial_runner(transport: SessionTransport, spec: SessionSpec) -> None:
    del spec
    await transport.send(make_msg_chunk(1, {"x": 1}))
    await transport.send(
        make_session_end(2, "completed", tokens={}, cost_usd=0.0)
    )


def _factory() -> Any:
    def _build(
        kind: str,
        policy: ToolPolicy,
        coordinator: HITLCoordinator,
        session_id: str,
        **kwargs: object,
    ) -> ExecutorBackend:
        del kind, policy, coordinator, session_id, kwargs
        return InProcessExecutor(runner=_trivial_runner)

    return _build


def _bcrypt_hash(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")


def _make_cfg(tmp_path: Path) -> Config:
    """Build a Config with ``alice`` configured as a dashboard user.

    ``role_mapping`` stays empty so ``dashboard-alice`` resolves to
    ``viewer`` on the *dashboard* path — same shape used by
    ``test_audit_dashboard_render``. The autouse conftest patch only
    fires when ``request.state.api_key_label`` is set, which the
    dashboard router does NOT do, so this gives us a deterministic
    "non-admin viewer" identity for the visibility / author-guard
    assertions.
    """
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/comments-dash.db"
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
async def client_and_store(tmp_path: Path):
    cfg = _make_cfg(tmp_path)
    app = create_app(cfg)
    app.state.executor_factory_override = _factory()

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


async def _seed_session(
    store: SqlAlchemyStore, *, sid: str, owner: str
) -> None:
    await store.create_session(
        id=sid,
        spec_json={"prompt": "seed"},
        trace_id=None,
        backend="inprocess",
        tags=(),
        owner=owner,
    )


async def test_dashboard_comments_renders_existing(
    client_and_store,
) -> None:
    """Two seeded comments → 200 + both ``<li class='comment-item'>``
    rows visible with author + body_html, plus the post form is
    appended below."""
    client, store = client_and_store
    await _seed_session(store, sid="sess-c1", owner="dashboard-alice")
    await store.create_comment(
        session_id="sess-c1",
        author="dashboard-alice",
        body_markdown="hello from alice",
        body_html="<p>hello from <strong>alice</strong></p>",
    )
    await store.create_comment(
        session_id="sess-c1",
        author="dashboard-bob",
        body_markdown="reply from bob",
        body_html="<p>reply from bob</p>",
    )

    r = await client.get("/dashboard/sessions/sess-c1/comments")
    assert r.status_code == 200, r.text
    body = r.text
    assert "comments-list-sess-c1" in body
    assert "comments-ul" in body
    assert body.count('class="comment-item"') == 2
    assert "dashboard-alice" in body
    assert "dashboard-bob" in body
    # Pre-sanitised body_html threaded through ``|safe`` so the
    # markup survives intact.
    assert "<strong>alice</strong>" in body
    assert "reply from bob" in body
    # The post form is always appended so a viewer can post a new
    # comment without an extra round trip.
    assert 'class="comment-form"' in body
    assert 'name="body"' in body
    assert 'hx-post="/api/v1/sessions/sess-c1/comments"' in body
    assert 'hx-ext="json-enc"' in body


async def test_dashboard_comments_empty_session_renders_form(
    client_and_store,
) -> None:
    """Session with zero comments → 200 + ``No comments yet.`` empty
    state + post form so the operator can still submit the first
    comment."""
    client, store = client_and_store
    await _seed_session(store, sid="sess-c2", owner="dashboard-alice")

    r = await client.get("/dashboard/sessions/sess-c2/comments")
    assert r.status_code == 200, r.text
    body = r.text
    assert "No comments yet." in body
    assert 'class="comment-form"' in body
    assert 'hx-post="/api/v1/sessions/sess-c2/comments"' in body


async def test_dashboard_comment_edit_form_author_only(
    client_and_store,
) -> None:
    """Edit form is gated by author match.

    * Bob's comment → alice (cookie session) gets 403 + ``Forbidden.``
      HTMX fragment.
    * Alice's own comment → 200 + textarea pre-filled with the
      original markdown so the inline edit can land verbatim.
    """
    client, store = client_and_store
    await _seed_session(store, sid="sess-c3", owner="dashboard-alice")

    bob_row = await store.create_comment(
        session_id="sess-c3",
        author="dashboard-bob",
        body_markdown="bob notes here",
        body_html="<p>bob notes here</p>",
    )
    cid_bob = int(bob_row["id"])
    r = await client.get(f"/dashboard/comments/{cid_bob}/edit")
    assert r.status_code == 403, r.text
    assert "Forbidden." in r.text

    # Alice's own comment — use a no-special-chars body so the assertion
    # doesn't have to track Jinja's autoescape quoting (e.g. ``'`` →
    # ``&#39;``); the autoescape itself is the correct behaviour, we
    # just don't want the test to be coupled to its exact quoting.
    alice_markdown = "alice notes here"
    alice_row = await store.create_comment(
        session_id="sess-c3",
        author="dashboard-alice",
        body_markdown=alice_markdown,
        body_html=f"<p>{alice_markdown}</p>",
    )
    cid_alice = int(alice_row["id"])
    r2 = await client.get(f"/dashboard/comments/{cid_alice}/edit")
    assert r2.status_code == 200, r2.text
    body = r2.text
    assert f'id="comment-{cid_alice}"' in body
    assert alice_markdown in body
    assert 'name="body"' in body
    assert f'hx-patch="/api/v1/comments/{cid_alice}"' in body
