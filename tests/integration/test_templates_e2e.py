"""Prompt template end-to-end tests — Plan 8 D8.24 / Task 14.

Black-box tests through ``httpx.AsyncClient`` against a live FastAPI
app (mirrors :mod:`tests.integration.test_favorites_e2e`). Each test
seeds its own role mapping so the root conftest's "empty
role_mapping → admin" autouse patch sleeps and the production role
enforcement kicks in.

Covered:

  * ``test_create_template_writes_audit`` — POST → 201 + a
    ``template_create`` audit row attributed to alice.
  * ``test_list_includes_shared_others_excludes_private_others`` —
    bob sees alice's shared template but not alice's private one.
  * ``test_update_template_creator_only_403_for_other`` — alice
    creates → bob PATCH → 403 ``forbidden_template_edit``.
  * ``test_delete_template_admin_can_delete_other`` — alice
    creates → admin DELETE → 204 + the row is gone.
  * ``test_unique_name_conflict_409`` — POST twice with the same
    ``(creator, name)`` → second call returns 409
    ``template_name_conflict``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from gg_relay.api.main import create_app
from gg_relay.config import Config


def _make_cfg(
    tmp_path: Path,
    *,
    api_keys_raw: str,
    role_mapping_raw: str,
) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/templates.db"
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


async def _create(
    client: AsyncClient,
    *,
    key: str,
    name: str,
    prompt: str = "hello",
    shared: bool = False,
    description: str | None = None,
    tags: str | None = None,
) -> Any:
    body: dict[str, Any] = {
        "name": name,
        "prompt": prompt,
        "shared": shared,
    }
    if description is not None:
        body["description"] = description
    if tags is not None:
        body["tags"] = tags
    return await client.post(
        "/api/v1/templates",
        json=body,
        headers={"X-API-Key": key},
    )


async def test_create_template_writes_audit(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """POST → 201 + a ``template_create`` audit row attributed to alice."""
    del tmp_path
    client, app = await app_factory(
        "alice=alice-key",
        "alice=submitter",
    )
    r = await _create(
        client,
        key="alice-key",
        name="deploy-prod",
        prompt="canary first",
        shared=True,
        description="prod deploy template",
        tags="ci,deploy",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "deploy-prod"
    assert body["creator"] == "alice"
    assert body["shared"] is True
    assert body["tags"] == "ci,deploy"
    tid = int(body["id"])

    store = app.state.store
    rows, _ = await store.list_audit(
        actor="alice", action="template_create", limit=10
    )
    assert len(rows) == 1, f"expected 1 template_create row, got {rows!r}"
    assert rows[0]["target_type"] == "template"
    assert rows[0]["target_id"] == str(tid)
    metadata = rows[0]["metadata_json"] or {}
    assert metadata.get("name") == "deploy-prod"
    assert metadata.get("shared") is True


async def test_list_includes_shared_others_excludes_private_others(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """Bob sees alice's shared template but not alice's private one."""
    del tmp_path
    client, _app = await app_factory(
        "alice=alice-key,bob=bob-key",
        "alice=submitter,bob=submitter",
    )
    r1 = await _create(
        client, key="alice-key", name="alice-private", shared=False
    )
    assert r1.status_code == 201, r1.text
    r2 = await _create(
        client, key="alice-key", name="alice-shared", shared=True
    )
    assert r2.status_code == 201, r2.text

    g = await client.get(
        "/api/v1/templates",
        headers={"X-API-Key": "bob-key"},
    )
    assert g.status_code == 200, g.text
    names = {it["name"] for it in g.json()["items"]}
    assert "alice-shared" in names
    assert "alice-private" not in names, (
        "non-admin must not see another user's private template"
    )

    # Non-admin bob cannot probe alice's private id directly either.
    private_id = r1.json()["id"]
    g2 = await client.get(
        f"/api/v1/templates/{private_id}",
        headers={"X-API-Key": "bob-key"},
    )
    assert g2.status_code == 403, g2.text
    assert g2.json()["detail"]["code"] == "forbidden_template_view"


async def test_update_template_creator_only_403_for_other(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """alice creates → bob PATCH → 403 ``forbidden_template_edit``."""
    del tmp_path
    client, _app = await app_factory(
        "alice=alice-key,bob=bob-key",
        "alice=submitter,bob=submitter",
    )
    r = await _create(
        client, key="alice-key", name="alice-tpl", shared=True
    )
    assert r.status_code == 201
    tid = r.json()["id"]

    p = await client.patch(
        f"/api/v1/templates/{tid}",
        json={"prompt": "bob's edit"},
        headers={"X-API-Key": "bob-key"},
    )
    assert p.status_code == 403, p.text
    assert p.json()["detail"]["code"] == "forbidden_template_edit"

    # The creator may still edit.
    p_alice = await client.patch(
        f"/api/v1/templates/{tid}",
        json={"prompt": "alice's edit"},
        headers={"X-API-Key": "alice-key"},
    )
    assert p_alice.status_code == 200, p_alice.text
    assert p_alice.json()["prompt"] == "alice's edit"


async def test_delete_template_admin_can_delete_other(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """alice creates → admin DELETE → 204 + the row is gone."""
    del tmp_path
    client, _app = await app_factory(
        "alice=alice-key,boss=boss-key",
        "alice=submitter,boss=admin",
    )
    r = await _create(
        client, key="alice-key", name="will-go", shared=True
    )
    assert r.status_code == 201
    tid = r.json()["id"]

    d = await client.delete(
        f"/api/v1/templates/{tid}",
        headers={"X-API-Key": "boss-key"},
    )
    assert d.status_code == 204, d.text

    # GET now 404s.
    g = await client.get(
        f"/api/v1/templates/{tid}",
        headers={"X-API-Key": "alice-key"},
    )
    assert g.status_code == 404, g.text
    assert g.json()["detail"]["code"] == "template_not_found"


async def test_unique_name_conflict_409(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    """Same creator + same name twice → second POST returns 409."""
    del tmp_path
    client, _app = await app_factory(
        "alice=alice-key",
        "alice=submitter",
    )
    r1 = await _create(client, key="alice-key", name="dup-name")
    assert r1.status_code == 201

    r2 = await _create(client, key="alice-key", name="dup-name")
    assert r2.status_code == 409, r2.text
    detail = r2.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "template_name_conflict"
