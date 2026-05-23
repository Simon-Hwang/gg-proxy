"""SessionManager.retry() unit tests — Plan 8 D8.6 / Task 9.

Exercises the retry path in isolation:

  * ``test_retry_creates_new_session_with_parent`` — happy path:
    submit → retry → new sid carries ``parent_session_id`` pointing
    at the original.
  * ``test_retry_copies_owner_and_tags`` — owner + tags + description
    propagate from the original to the retry.
  * ``test_retry_raises_when_original_has_no_prompt`` — guard clause:
    a hand-crafted spec_json missing ``prompt`` triggers
    :class:`gg_relay.core.RetryConfigError` instead of submitting
    a degenerate session.

Fixtures reuse :mod:`tests.unit.session.test_manager` patterns — same
``trivial_runner`` + same in-process executor factory so the runner
publishes a ``session.end`` immediately and the test can assert on
the persisted row without waiting on real work.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from gg_relay.core import EventBus, RetryConfigError, SessionState
from gg_relay.redaction import RedactionEngine
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.manager import (
    SessionManager,
    SessionNotFound,
)
from gg_relay.session.spec import PluginManifest, SessionSpec
from gg_relay.store import SessionRepository, create_all_tables, make_async_engine

from .test_manager import (
    FakeAssembler,
    make_factory,
    runner_factory_trivial,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def store_engine(tmp_path: Path):
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/_retry.db")
    await create_all_tables(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def manager(store_engine, tmp_path: Path) -> SessionManager:
    store = SessionRepository(store_engine)
    return SessionManager(
        executor_factory=make_factory(runner_factory_trivial),
        assembler=FakeAssembler(),
        store=store,
        bus=EventBus(),
        coordinator=HITLCoordinator(),
        redactor=RedactionEngine(),
        default_policy=ToolPolicy(),
        install_dir_root=tmp_path / "installs",
        default_timeout_s=2,
        max_concurrent=4,
        grace_period_s=1,
    )


def _make_spec(tmp_path: Path, *, tags: tuple[str, ...] = ()) -> SessionSpec:
    return SessionSpec(
        prompt="retry me",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
        timeout_s=2,
        tags=tags,
    )


async def _wait_for_terminal(
    manager: SessionManager, sid: str, *, timeout: float = 3.0
) -> None:
    """Block until the session reaches a terminal state — the row's
    ``end_reason`` is what we want to assert on after the retry."""
    deadline = asyncio.get_running_loop().time() + timeout
    terminal = {
        SessionState.COMPLETED,
        SessionState.CANCELLED,
        SessionState.FAILED,
    }
    while True:
        det = await manager.get(sid)
        if det.status in terminal:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"timed out waiting for {sid} to terminate; last={det.status}"
            )
        await asyncio.sleep(0.02)


class TestRetryHappyPath:
    async def test_retry_creates_new_session_with_parent(
        self, manager: SessionManager, tmp_path: Path
    ) -> None:
        original_sid = await manager.submit(
            _make_spec(tmp_path), owner="alice"
        )
        await _wait_for_terminal(manager, original_sid)

        new_sid = await manager.retry(original_sid, actor="alice")
        assert new_sid != original_sid

        # The store row for the new session must carry parent_session_id
        # pointing at the original.
        store = manager._store  # noqa: SLF001 — internal test access
        new_row = await store.get_session(new_sid)
        assert new_row is not None
        assert new_row["parent_session_id"] == original_sid

        # And :meth:`list_children_of_session` should resolve the link
        # the other way around.
        children = await store.list_children_of_session(
            parent_session_id=original_sid
        )
        assert [r["id"] for r in children] == [new_sid]

    async def test_retry_copies_owner_and_tags(
        self, manager: SessionManager, tmp_path: Path
    ) -> None:
        original_sid = await manager.submit(
            _make_spec(tmp_path, tags=("alpha", "beta")),
            owner="alice",
            description="original work",
        )
        await _wait_for_terminal(manager, original_sid)

        # No actor → fall back to the original session's owner.
        new_sid = await manager.retry(original_sid)
        store = manager._store  # noqa: SLF001
        new_row = await store.get_session(new_sid)
        assert new_row is not None
        assert new_row["owner"] == "alice"
        # Tags travel through ``spec.tags`` → ``store.create_session(tags=...)``.
        assert tuple(new_row["tags"] or ()) == ("alpha", "beta")
        # Original description survives onto the retry so the dashboard
        # can keep showing the same human-readable label.
        assert new_row["description"] == "original work"


class TestRetryFailureModes:
    async def test_retry_raises_when_original_has_no_prompt(
        self, manager: SessionManager, tmp_path: Path
    ) -> None:
        """Hand-craft a sessions row whose spec_json is missing
        ``prompt`` so the guard clause in
        :meth:`SessionManager.retry` triggers."""
        store = manager._store  # noqa: SLF001
        await store.create_session(
            id="malformed",
            spec_json={"plugins": {"profile": "minimal"}},  # no prompt
            trace_id=None,
            backend="inprocess",
            owner="alice",
        )
        with pytest.raises(RetryConfigError) as ei:
            await manager.retry("malformed", actor="alice")
        assert "no prompt" in str(ei.value).lower()

    async def test_retry_unknown_session_raises_not_found(
        self, manager: SessionManager
    ) -> None:
        """Defensive — retrying a sid that doesn't exist must surface
        :class:`SessionNotFound` rather than silently submitting an
        empty session (no spec_json to read)."""
        with pytest.raises(SessionNotFound):
            await manager.retry("does-not-exist", actor="alice")
