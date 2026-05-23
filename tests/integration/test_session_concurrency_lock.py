"""Plan 7 D7.5 / Task 8 — session pause/resume optimistic-locking races.

Two integration tests covering the full pause flow with concurrency:

* :func:`test_two_pause_same_sid_race` — two concurrent ``pause()`` calls
  on the same session. At least one must succeed; the manager's in-process
  serialisation (``self._paused_set`` membership check) collapses the
  redundant call. Final state must settle at ``paused`` with a strictly
  positive ``version`` (every successful write bumps the counter).
* :func:`test_concurrency_error_returns_409` — direct row mutation forces
  the version-check to fail; the API router emits ``409`` with
  ``code=session_version_mismatch``.

Both reuse the ``test_api_pause_resume`` style harness (real FastAPI app,
``InProcessExecutor`` driven by a blocking runner, mock bridge swapped
into ``manager._bridges``) so the entire dependency chain (router →
manager → store → bridge) is exercised.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.session.control import ControlAck
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.executor.protocol import ExecutorBackend
from gg_relay.session.frames import make_msg_chunk, make_session_end
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.manager import SessionManager
from gg_relay.session.runner.inprocess_control import InProcessBridge
from gg_relay.session.spec import SessionSpec
from gg_relay.session.transport.protocol import SessionTransport

pytestmark = pytest.mark.asyncio

HEADERS = {"X-API-Key": "k1"}


@dataclass
class _BlockingRunner:
    """Holds the session in RUNNING until ``released`` is set."""

    released: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(
        self, transport: SessionTransport, spec: SessionSpec
    ) -> None:
        del spec
        await transport.send(make_msg_chunk(1, {"start": True}))
        await self.released.wait()
        await transport.send(
            make_session_end(2, "completed", tokens={}, cost_usd=0.0)
        )


@dataclass
class _SlowAckBridge:
    """Bridge mock whose ``pause`` yields the event loop once before ack.

    Sleeping for a tiny tick lets the two concurrent pause() coroutines
    both pass the ``self._paused_set`` membership check and reach the
    version-checked DB write, which is the race we want to cover.
    """

    pause_calls: list[str | None] = field(default_factory=list)
    resume_calls: list[str | None] = field(default_factory=list)

    async def pause(self, *, reason: str | None = None) -> ControlAck:
        self.pause_calls.append(reason)
        await asyncio.sleep(0)
        return ControlAck(
            op="pause", req_id=f"p-{len(self.pause_calls)}", ok=True
        )

    async def resume(self, *, hint: str | None = None) -> ControlAck:
        self.resume_calls.append(hint)
        await asyncio.sleep(0)
        return ControlAck(
            op="resume", req_id=f"r-{len(self.resume_calls)}", ok=True
        )


@dataclass
class _State:
    runners: list[_BlockingRunner] = field(default_factory=list)


def _make_factory(state: _State) -> Callable[..., ExecutorBackend]:
    def _factory(
        kind: str,
        policy: ToolPolicy,
        coordinator: HITLCoordinator,
        session_id: str,
        **kwargs: object,
    ) -> ExecutorBackend:
        del kind, policy, coordinator, session_id, kwargs
        runner = _BlockingRunner()
        state.runners.append(runner)
        return InProcessExecutor(runner=runner)

    return _factory


def _make_cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/lock.db"
    cfg.api_keys_raw = "k1"
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://localhost:8000"
    cfg.default_timeout_s = 10
    cfg.grace_period_s = 1
    cfg.max_concurrent_sessions = 4
    cfg.max_paused = 5
    cfg.max_paused_per_api_key = 4
    cfg.paused_timeout_s = 60
    cfg.resume_timeout_s = 0.5
    return cfg


@pytest_asyncio.fixture
async def state() -> _State:
    return _State()


@pytest_asyncio.fixture
async def client(
    tmp_path: Path, state: _State
) -> AsyncIterator[tuple[AsyncClient, SessionManager, Config]]:
    cfg = _make_cfg(tmp_path)
    app = create_app(cfg)
    app.state.executor_factory_override = _make_factory(state)
    from gg_relay.store import create_all_tables, make_async_engine

    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as ac, app.router.lifespan_context(app):
        manager: SessionManager = app.state.manager
        yield ac, manager, cfg
        for runner in state.runners:
            runner.released.set()


def _spec_body(tmp_path: Path) -> dict[str, Any]:
    return {
        "spec": {
            "prompt": "hello",
            "cwd": str(tmp_path),
            "plugins": {"profile": "minimal"},
            "executor": "inprocess",
            "timeout_s": 10,
            "tags": [],
        },
        "credentials": {},
    }


async def _submit_and_wait_running(
    ac: AsyncClient,
    manager: SessionManager,
    tmp_path: Path,
) -> str:
    r = await ac.post(
        "/api/v1/sessions", json=_spec_body(tmp_path), headers=HEADERS
    )
    assert r.status_code == 202, r.text
    sid = r.json()["id"]
    deadline = asyncio.get_running_loop().time() + 2.0
    while True:
        det = await manager.get(sid)
        if det.status.value == "running":
            return cast(str, sid)
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"{sid} never RUNNING; last={det.status}")
        await asyncio.sleep(0.01)


def _install_bridge(
    manager: SessionManager, sid: str, bridge: _SlowAckBridge
) -> None:
    manager._bridges[sid] = cast(InProcessBridge, bridge)


# ── tests ──────────────────────────────────────────────────────────


async def test_two_pause_same_sid_race(
    client: tuple[AsyncClient, SessionManager, Config], tmp_path: Path
) -> None:
    """Two concurrent pause() calls collapse to one durable transition.

    Plan 7 D7.5 expectation: the second pause is absorbed by either
    (a) the in-process ``_paused_set`` idempotent return, or
    (b) the version-checked DB write surfacing
    :class:`gg_relay.store.exceptions.ConcurrencyError`.
    Either way the final row state must be ``paused`` and ``version``
    must have strictly increased from its pre-pause anchor.
    """
    ac, manager, _cfg = client
    sid = await _submit_and_wait_running(ac, manager, tmp_path)
    bridge = _SlowAckBridge()
    _install_bridge(manager, sid, bridge)

    # Capture the pre-pause version so the post-race assertion can
    # confirm at least one durable bump happened.
    row_before = await manager._store.get_session(sid)
    assert row_before is not None
    v_before = int(row_before["version"])

    results = await asyncio.gather(
        manager.pause(sid, reason="r1"),
        manager.pause(sid, reason="r2"),
        return_exceptions=True,
    )
    # At least one of the two coroutines must have returned None
    # (success); failures, if any, must be ConcurrencyError (NOT a
    # different unexpected exception class).
    successes = [r for r in results if r is None]
    failures = [r for r in results if isinstance(r, BaseException)]
    assert successes, f"both pause() calls failed: {results!r}"
    from gg_relay.store import ConcurrencyError

    for f in failures:
        assert isinstance(f, ConcurrencyError), (
            f"unexpected exception type {type(f).__name__}: {f!r}"
        )

    det = await manager.get(sid)
    assert det.status.value == "paused"
    row_after = await manager._store.get_session(sid)
    assert row_after is not None
    assert int(row_after["version"]) > v_before


async def test_concurrency_error_returns_409(
    client: tuple[AsyncClient, SessionManager, Config], tmp_path: Path
) -> None:
    """Stale version forces 409 with ``code=session_version_mismatch``.

    Mutates the row's ``version`` directly between the manager's read
    and write so both the initial attempt AND the 1-retry attempt
    collide; the API router then surfaces the configured 409 body.
    """
    ac, manager, cfg = client
    sid = await _submit_and_wait_running(ac, manager, tmp_path)
    bridge = _SlowAckBridge()
    _install_bridge(manager, sid, bridge)

    # Patch ``store.update_session_status`` so it bumps the row's
    # version *before* each call when the pause transition is the one
    # in flight. The optimistic-lock helper does 1 jitter retry → 2
    # store calls; bumping the version each time forces BOTH to lose
    # the WHERE-version filter and exhaust the retry budget, surfacing
    # as a 409 at the API layer.
    from gg_relay.store import ConcurrencyError, make_async_engine

    eng = make_async_engine(cfg.database_url)
    store = manager._store
    original_update = store.update_session_status

    async def _bump_then_call(
        sid_in: str, **kwargs: Any
    ) -> int:
        # Only force-fail the externally-driven pause transition so
        # the suppressed ``_run`` lifecycle writes (status=running,
        # status=completed) keep working.
        if kwargs.get("status") == "paused":
            async with eng.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE sessions SET version = version + 1 "
                        "WHERE id = :id"
                    ),
                    {"id": sid_in},
                )
        return await original_update(sid_in, **kwargs)

    store.update_session_status = _bump_then_call  # type: ignore[assignment]
    try:
        r = await ac.post(
            f"/api/v1/sessions/{sid}/pause",
            json={"reason": "racy"},
            headers=HEADERS,
        )
    finally:
        store.update_session_status = original_update  # type: ignore[assignment]
        await eng.dispose()

    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "session_version_mismatch"
    assert "refresh" in body["detail"].lower()
    # Sanity: ConcurrencyError is the exception class we wired in.
    assert ConcurrencyError.__name__ == "ConcurrencyError"
