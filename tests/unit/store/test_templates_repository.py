"""Unit tests for SqlAlchemyStore prompt-template methods — Plan 8 D8.24 / Task 14.

Exercises the new :meth:`SqlAlchemyStore.create_template` /
:meth:`get_template` / :meth:`list_templates` /
:meth:`update_template` / :meth:`delete_template` surface. Fixture
pattern mirrors :mod:`tests.unit.store.test_favorites_repository` —
a fresh on-disk SQLite per test under ``tmp_path`` so the
``Boolean`` storage / unique constraint behaviour matches production.

Tests:

  * ``test_create_template_unique_per_creator_name`` — same
    creator + same name twice → :class:`TemplateConflictError`;
    different creator + same name works fine.
  * ``test_list_templates_filters_private_others`` — two users
    each seed one private + one shared template; non-admin sees
    own + shared = 3 rows; admin with ``include_others`` sees all 4.
  * ``test_update_template_changes_shared_visibility`` — flipping
    ``shared`` True → False removes the row from another user's
    list response.
  * ``test_delete_template_removes_row`` — :meth:`delete_template`
    returns True on hit and False on miss; a deleted id is gone
    from the listing.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from gg_relay.core.exceptions import TemplateConflictError
from gg_relay.store import (
    SqlAlchemyStore,
    create_all_tables,
    make_async_engine,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncEngine:
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/templates.db")
    await create_all_tables(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def store(engine: AsyncEngine) -> SqlAlchemyStore:
    return SqlAlchemyStore(engine)


class TestCreateTemplate:
    async def test_create_template_unique_per_creator_name(
        self, store: SqlAlchemyStore
    ) -> None:
        """Same creator + same name twice → TemplateConflictError;
        a different creator may reuse the same name."""
        await store.create_template(
            name="deploy-prod",
            creator="alice",
            prompt="run the prod deploy",
            description="canary first",
            shared=False,
            tags="ci,deploy",
        )

        with pytest.raises(TemplateConflictError) as excinfo:
            await store.create_template(
                name="deploy-prod",
                creator="alice",
                prompt="duplicate",
            )
        assert "deploy-prod" in str(excinfo.value)
        assert "alice" in str(excinfo.value)

        # Different creator may reuse the same name — per-creator
        # namespace, not global.
        bob_row = await store.create_template(
            name="deploy-prod",
            creator="bob",
            prompt="bob's deploy",
        )
        assert bob_row["creator"] == "bob"
        assert bob_row["name"] == "deploy-prod"


class TestListTemplates:
    async def test_list_templates_filters_private_others(
        self, store: SqlAlchemyStore
    ) -> None:
        """Two users seed one private + one shared template each.

        Non-admin alice sees alice's two templates + bob's shared =
        3 rows (the shared row floats to the top via
        ``shared DESC``). Admin alice with ``include_others=True``
        also sees bob's private template = 4 rows.
        """
        await store.create_template(
            name="alice-private",
            creator="alice",
            prompt="alice scratch",
            shared=False,
        )
        await store.create_template(
            name="alice-shared",
            creator="alice",
            prompt="alice public",
            shared=True,
        )
        await store.create_template(
            name="bob-private",
            creator="bob",
            prompt="bob scratch",
            shared=False,
        )
        await store.create_template(
            name="bob-shared",
            creator="bob",
            prompt="bob public",
            shared=True,
        )

        alice_view = await store.list_templates(
            actor="alice", is_admin=False
        )
        names = {r["name"] for r in alice_view}
        assert names == {"alice-private", "alice-shared", "bob-shared"}, (
            f"alice should not see bob's private template: {names}"
        )
        # shared rows float to the top (shared DESC, name ASC).
        assert bool(alice_view[0]["shared"]) is True

        # Admin without include_others gets the same view as a non-
        # admin user — the default is the safe one.
        admin_default = await store.list_templates(
            actor="alice", is_admin=True, include_others=False
        )
        assert {r["name"] for r in admin_default} == {
            "alice-private",
            "alice-shared",
            "bob-shared",
        }

        # Admin with include_others=True sees every row.
        admin_all = await store.list_templates(
            actor="alice", is_admin=True, include_others=True
        )
        assert {r["name"] for r in admin_all} == {
            "alice-private",
            "alice-shared",
            "bob-private",
            "bob-shared",
        }


class TestUpdateTemplate:
    async def test_update_template_changes_shared_visibility(
        self, store: SqlAlchemyStore
    ) -> None:
        """Flipping ``shared`` True → False removes the template from
        another user's list response."""
        row = await store.create_template(
            name="ci-template",
            creator="alice",
            prompt="run ci",
            shared=True,
        )
        tid = int(row["id"])

        # Bob (non-admin) sees the shared template.
        bob_before = await store.list_templates(
            actor="bob", is_admin=False
        )
        assert any(r["id"] == tid for r in bob_before)

        ok = await store.update_template(
            template_id=tid,
            shared=False,
        )
        assert ok is True

        # After the flip, bob's view no longer includes the row.
        bob_after = await store.list_templates(
            actor="bob", is_admin=False
        )
        assert all(r["id"] != tid for r in bob_after), (
            "shared=False template still visible to non-creator"
        )

        # Alice (the creator) still sees it regardless of the flag.
        alice_after = await store.list_templates(
            actor="alice", is_admin=False
        )
        assert any(r["id"] == tid for r in alice_after)


class TestDeleteTemplate:
    async def test_delete_template_removes_row(
        self, store: SqlAlchemyStore
    ) -> None:
        """delete_template returns True on hit, False on miss; the
        deleted id is gone from the listing AND from get_template."""
        row = await store.create_template(
            name="ephemeral",
            creator="alice",
            prompt="will be deleted",
        )
        tid = int(row["id"])

        gone = await store.delete_template(template_id=tid)
        assert gone is True

        # get_template now returns None.
        assert await store.get_template(template_id=tid) is None

        # A second delete of the same id returns False — the row no
        # longer matches.
        gone_again = await store.delete_template(template_id=tid)
        assert gone_again is False

        # And the listing no longer surfaces it.
        rows = await store.list_templates(actor="alice")
        assert all(r["id"] != tid for r in rows)
