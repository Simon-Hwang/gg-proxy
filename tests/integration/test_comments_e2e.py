"""Session comments end-to-end tests — Plan 8 D8.5 / Task 7.

Black-box tests through ``httpx.AsyncClient`` against a live
FastAPI app (mirrors :mod:`tests.integration.test_role_endpoint_e2e`).
Each test seeds its own role mapping so the root conftest's
"empty role_mapping → admin" autouse patch sleeps and the
production role enforcement kicks in.

Covered:

  * ``test_create_get_round_trip`` — POST → GET returns the comment
    with sanitised HTML.
  * ``test_xss_payload_sanitized_through_endpoint`` — POST with a
    ``<script>`` body must persist a body_html that does NOT contain
    the live tag.
  * ``test_only_author_can_edit`` — alice creates → bob PATCH → 403
    with ``forbidden_comment_edit``.
  * ``test_author_can_delete`` — alice creates → alice DELETE → 204.
  * ``test_admin_can_delete_others_comment`` — alice (submitter)
    creates → admin DELETE → 204.
  * ``test_cascade_delete_with_session`` — deleting the parent
    session cascades to its comments via the FK ON DELETE CASCADE.
  * ``test_audit_log_records_create_update_delete`` — every mutation
    writes an audit row with the matching ``action``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.executor.protocol import ExecutorBackend
from gg_relay.session.frames import make_msg_chunk, make_session_end
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.spec import SessionSpec
from gg_relay.session.transport.protocol import SessionTransport


async def _trivial_runner(transport: SessionTransport, spec: SessionSpec) -> None:
    """Drain immediately — the test only needs the session row to
    materialise so the comments router can find its parent."""
    del spec
    await transport.send(make_msg_chunk(1, {"x": 1}))
    await transport.send(make_session_end(2, "completed", tokens={}, cost_usd=0.0))


def _factory_override() -> Callable[..., ExecutorBackend]:
    def _factory(
        kind: str,
        policy: ToolPolicy,
        coordinator: HITLCoordinator,
        session_id: str,
        **kwargs: object,
    ) -> ExecutorBackend:
        del kind, policy, coordinator, session_id, kwargs
        return InProcessExecutor(runner=_trivial_runner)

    return _factory


def _make_cfg(
    tmp_path: Path,
    *,
    api_keys_raw: str,
    role_mapping_raw: str,
) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/comments.db"
    cfg.api_keys_raw = api_keys_raw
    cfg.role_mapping_raw = role_mapping_raw
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://localhost:8000"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


@pytest_asyncio.fixture
async def app_factory(
    tmp_path: Path,
) -> AsyncIterator[Callable[[str, str], Any]]:
    """Factory yielding ``AsyncClient`` instances pinned to a fresh
    app per (api_keys_raw, role_mapping_raw) tuple."""
    clients: list[Any] = []

    async def _make(
        api_keys_raw: str,
        role_mapping_raw: str,
    ) -> tuple[AsyncClient, Any]:
        cfg = _make_cfg(
            tmp_path,
            api_keys_raw=api_keys_raw,
            role_mapping_raw=role_mapping_raw,
        )
        app = create_app(cfg)
        app.state.executor_factory_override = _factory_override()
        from gg_relay.store import create_all_tables, make_async_engine

        eng = make_async_engine(cfg.database_url)
        await create_all_tables(eng)
        await eng.dispose()
        transport = ASGITransport(app=app)
        client_ctx = AsyncClient(transport=transport, base_url="http://test")
        lifespan_ctx = app.router.lifespan_context(app)
        await lifespan_ctx.__aenter__()
        client = await client_ctx.__aenter__()
        clients.append((client_ctx, lifespan_ctx))
        return client, app

    yield _make

    for client_ctx, lifespan_ctx in clients:
        await client_ctx.__aexit__(None, None, None)
        await lifespan_ctx.__aexit__(None, None, None)


def _spec_body(tmp_path: Path) -> dict[str, Any]:
    return {
        "spec": {
            "prompt": "hello",
            "cwd": str(tmp_path),
            "plugins": {"profile": "minimal"},
            "executor": "inprocess",
            "timeout_s": 5,
            "tags": [],
        },
        "credentials": {},
    }


async def _submit_session(
    client: AsyncClient, tmp_path: Path, key: str
) -> str:
    """Submit a session via the API and return the session id.

    Drives the same path as production so the parent FK constraint
    on ``session_comments`` is satisfied with a row that actually
    landed via the SessionManager lifecycle.
    """
    r = await client.post(
        "/api/v1/sessions",
        json=_spec_body(tmp_path),
        headers={"X-API-Key": key},
    )
    assert r.status_code == 202, r.text
    return r.json()["id"]


async def test_create_get_round_trip(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """POST a comment + GET the list back. The sanitised HTML must
    contain the markdown-rendered body, not raw markdown."""
    client, _app = await app_factory(
        "alice=alice-key",
        "alice=submitter",
    )
    sid = await _submit_session(client, tmp_path, "alice-key")

    r = await client.post(
        f"/api/v1/sessions/{sid}/comments",
        json={"body": "**hello** world"},
        headers={"X-API-Key": "alice-key"},
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["author"] == "alice"
    assert created["body_markdown"] == "**hello** world"
    assert "<strong>hello</strong>" in created["body_html"]

    g = await client.get(
        f"/api/v1/sessions/{sid}/comments",
        headers={"X-API-Key": "alice-key"},
    )
    assert g.status_code == 200
    items = g.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == created["id"]
    assert items[0]["body_markdown"] == "**hello** world"


async def test_xss_payload_sanitized_through_endpoint(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """A POST body carrying ``<script>`` must NOT produce a body_html
    that contains a live script tag — the bleach pipeline removes it
    before the row is persisted."""
    client, _app = await app_factory(
        "alice=alice-key",
        "alice=submitter",
    )
    sid = await _submit_session(client, tmp_path, "alice-key")

    # Mix three classic XSS vectors in a single payload. The raw
    # ``<a href="javascript:...">`` form is used instead of the
    # markdown ``[text](url)`` form because ``markdown_it`` pre-filters
    # dangerous protocols at parse time — to actually exercise the
    # bleach allow-list we need to send raw HTML.
    payload = (
        "Hi <script>alert('xss')</script> there "
        "<img src=x onerror=alert(1)> "
        '<a href="javascript:alert(1)">click</a>'
    )
    r = await client.post(
        f"/api/v1/sessions/{sid}/comments",
        json={"body": payload},
        headers={"X-API-Key": "alice-key"},
    )
    assert r.status_code == 201, r.text
    created = r.json()
    # Markdown is preserved verbatim.
    assert created["body_markdown"] == payload
    # The sanitised HTML must not carry any LIVE attack vectors.
    # ``<script>`` and ``<img>`` are dropped at the tag level; the
    # ``<a href="javascript:...">`` href is stripped (the ``<a>``
    # tag may survive without a navigable target).
    html = created["body_html"].lower()
    assert "<script" not in html
    assert "<img" not in html
    assert "onerror" not in html
    assert 'href="javascript:' not in html
    # Sanity — surrounding sentence survives.
    assert "hi" in html
    assert "there" in html


async def test_only_author_can_edit(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """Alice creates a comment → Bob PATCHes → 403."""
    client, _app = await app_factory(
        "alice=alice-key,bob=bob-key",
        "alice=submitter,bob=submitter",
    )
    sid = await _submit_session(client, tmp_path, "alice-key")
    r = await client.post(
        f"/api/v1/sessions/{sid}/comments",
        json={"body": "from alice"},
        headers={"X-API-Key": "alice-key"},
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    # Bob attempts to edit — must 403 with the structured body.
    r2 = await client.patch(
        f"/api/v1/comments/{cid}",
        json={"body": "by bob"},
        headers={"X-API-Key": "bob-key"},
    )
    assert r2.status_code == 403, r2.text
    detail = r2.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "forbidden_comment_edit"
    assert detail["comment_author"] == "alice"
    assert detail["current_actor"] == "bob"


async def test_author_can_delete(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """Alice creates a comment → Alice DELETEs → 204; the comment
    disappears from the list."""
    client, _app = await app_factory(
        "alice=alice-key",
        "alice=submitter",
    )
    sid = await _submit_session(client, tmp_path, "alice-key")
    r = await client.post(
        f"/api/v1/sessions/{sid}/comments",
        json={"body": "delete me"},
        headers={"X-API-Key": "alice-key"},
    )
    cid = r.json()["id"]
    d = await client.delete(
        f"/api/v1/comments/{cid}", headers={"X-API-Key": "alice-key"}
    )
    assert d.status_code == 204, d.text
    # Subsequent GET must return an empty list (soft-deleted row hidden).
    g = await client.get(
        f"/api/v1/sessions/{sid}/comments",
        headers={"X-API-Key": "alice-key"},
    )
    assert g.json()["items"] == []


async def test_admin_can_delete_others_comment(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """Bob (submitter) creates → Alice (admin) DELETEs → 204."""
    client, _app = await app_factory(
        "alice=alice-key,bob=bob-key",
        "alice=admin,bob=submitter",
    )
    sid = await _submit_session(client, tmp_path, "bob-key")
    r = await client.post(
        f"/api/v1/sessions/{sid}/comments",
        json={"body": "from bob"},
        headers={"X-API-Key": "bob-key"},
    )
    cid = r.json()["id"]
    d = await client.delete(
        f"/api/v1/comments/{cid}", headers={"X-API-Key": "alice-key"}
    )
    assert d.status_code == 204, d.text


async def test_viewer_cannot_create_comment(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """The route depends on ``require_role('submitter')`` — a
    viewer POST must 403 with ``insufficient_role``."""
    client, _app = await app_factory(
        # Two keys so the viewer can probe AFTER a submitter has
        # created the parent session row (viewers can't submit).
        "alice=alice-key,vince=vince-key",
        "alice=submitter,vince=viewer",
    )
    sid = await _submit_session(client, tmp_path, "alice-key")
    r = await client.post(
        f"/api/v1/sessions/{sid}/comments",
        json={"body": "viewer try"},
        headers={"X-API-Key": "vince-key"},
    )
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "insufficient_role"
    assert detail["required_role"] == "submitter"
    assert detail["current_role"] == "viewer"


async def test_cascade_delete_with_session(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """Deleting the parent session must cascade to its comments.

    SQLite enforces ON DELETE CASCADE only when ``PRAGMA
    foreign_keys=ON`` is set; the project's :func:`make_async_engine`
    enables that pragma on connect, so the cascade is observable
    through the live API stack here.
    """
    client, app = await app_factory(
        "alice=alice-key",
        "alice=admin",
    )
    sid = await _submit_session(client, tmp_path, "alice-key")
    r = await client.post(
        f"/api/v1/sessions/{sid}/comments",
        json={"body": "soon to vanish"},
        headers={"X-API-Key": "alice-key"},
    )
    cid = r.json()["id"]

    # Drop the session directly through the store so we hit the
    # ``ON DELETE CASCADE`` path without depending on any specific
    # session-delete endpoint shape.
    store = app.state.store
    from gg_relay.store import session_comments, sessions

    engine = app.state.engine
    # Belt-and-braces: enable the FK pragma for THIS connection in
    # case make_async_engine's session-scoped pragma listener missed
    # this short-lived connection.
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA foreign_keys=ON"))
        await conn.execute(sessions.delete().where(sessions.c.id == sid))

    # Comment must be gone.
    async with engine.connect() as conn:
        await conn.execute(text("PRAGMA foreign_keys=ON"))
        row = (
            await conn.execute(
                session_comments.select().where(
                    session_comments.c.id == cid
                )
            )
        ).first()
    assert row is None, "cascade delete did not propagate to comment row"
    # Keep the unused reference to ``store`` so lint doesn't flag it.
    assert store is not None


async def test_audit_log_records_create_update_delete(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """Every comment mutation must write an audit row with the
    matching ``action``. The fallback middleware would also fire
    an ``unknown_mutation`` row if the inline write was missing —
    we assert the explicit actions are present and don't bother
    asserting on the fallback rows."""
    client, app = await app_factory(
        "alice=alice-key",
        "alice=admin",
    )
    sid = await _submit_session(client, tmp_path, "alice-key")

    r = await client.post(
        f"/api/v1/sessions/{sid}/comments",
        json={"body": "first"},
        headers={"X-API-Key": "alice-key"},
    )
    cid = r.json()["id"]
    p = await client.patch(
        f"/api/v1/comments/{cid}",
        json={"body": "edited"},
        headers={"X-API-Key": "alice-key"},
    )
    assert p.status_code == 200
    d = await client.delete(
        f"/api/v1/comments/{cid}", headers={"X-API-Key": "alice-key"}
    )
    assert d.status_code == 204

    store = app.state.store
    rows, _ = await store.list_audit(
        target_type="comment", target_id=str(cid), limit=100
    )
    actions = {r["action"] for r in rows}
    assert "comment_create" in actions
    assert "comment_update" in actions
    assert "comment_delete" in actions
    # Sanity — every row attributes the action to alice.
    for row in rows:
        if row["action"].startswith("comment_"):
            assert row["actor"] == "alice"
