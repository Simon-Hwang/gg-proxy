"""SessionManager pause/resume tests — Plan 6 Task 3.

These tests exercise the manager-level pause/resume semantics without
involving the real wire / SDK control loop. Instead, a :class:`MockBridge`
is injected into ``manager._bridges`` after the session reaches RUNNING,
so the assertions can focus on:

* state transitions persisted to the store
* semaphore release on pause / re-acquire on resume
* paused-timeout watchdog cancelling the session
* shutdown coordination per Plan 6 D6.15
* error mapping (SessionNotFound / SessionNotRunning / SessionNotPaused
  / ResumeQueueTimeout)

The MockBridge is structurally compatible with InProcessBridge /
WireBridge — Python doesn't care about the concrete type, so we use a
``cast`` purely to satisfy mypy.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio

from gg_relay.core import EventBus, SessionState, SessionStateChanged
from gg_relay.redaction import RedactionEngine
from gg_relay.session.control import ControlAck
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.executor.protocol import ExecutorBackend
from gg_relay.session.frames import make_msg_chunk, make_session_end
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.manager import (
    ResumeQueueTimeout,
    SessionManager,
    SessionNotFound,
    SessionNotPaused,
    SessionNotRunning,
)
from gg_relay.session.runner.inprocess_control import InProcessBridge
from gg_relay.session.spec import PluginManifest, SessionSpec
from gg_relay.session.transport.protocol import SessionTransport
from gg_relay.store import SessionRepository, create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


# ── fakes ─────────────────────────────────────────────────────────────


class _NoopAssembler:
    async def prepare(self, spec: SessionSpec, *, install_dir: Path) -> None:
        del spec, install_dir
        return None


@dataclass
class _BlockingRunner:
    """Runner coroutine that blocks until :meth:`release` is called.

    Sends a hello frame so the manager moves past ``submit`` and into
    ``RUNNING``, then waits forever (until the test explicitly releases
    or the session is cancelled). On release, sends a ``session.end``.
    """

    released: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(
        self, transport: SessionTransport, spec: SessionSpec
    ) -> None:
        del spec
        await transport.send(make_msg_chunk(1, {"type": "start"}))
        await self.released.wait()
        await transport.send(
            make_session_end(2, "completed", tokens={}, cost_usd=0.0)
        )


@dataclass
class _MockBridge:
    """Test double for InProcessBridge / WireBridge.

    Records every call so tests can assert exact pause/resume cadence.
    By default every ack succeeds; set ``pause_should_fail`` /
    ``resume_should_fail`` to flip ``ok=False`` with an error string, or
    ``pause_raises`` / ``resume_raises`` to raise instead.
    """

    pause_calls: list[str | None] = field(default_factory=list)
    resume_calls: list[str | None] = field(default_factory=list)
    pause_should_fail: str | None = None
    resume_should_fail: str | None = None
    pause_raises: BaseException | None = None
    resume_raises: BaseException | None = None
    pause_delay_s: float = 0.0

    async def pause(self, *, reason: str | None = None) -> ControlAck:
        self.pause_calls.append(reason)
        if self.pause_raises is not None:
            raise self.pause_raises
        if self.pause_delay_s:
            await asyncio.sleep(self.pause_delay_s)
        req_id = f"pause-{len(self.pause_calls)}"
        if self.pause_should_fail is not None:
            return ControlAck(
                op="pause", req_id=req_id, ok=False, error=self.pause_should_fail
            )
        return ControlAck(op="pause", req_id=req_id, ok=True)

    async def resume(self, *, hint: str | None = None) -> ControlAck:
        self.resume_calls.append(hint)
        if self.resume_raises is not None:
            raise self.resume_raises
        req_id = f"resume-{len(self.resume_calls)}"
        if self.resume_should_fail is not None:
            return ControlAck(
                op="resume",
                req_id=req_id,
                ok=False,
                error=self.resume_should_fail,
            )
        return ControlAck(op="resume", req_id=req_id, ok=True)


def _make_factory(
    runner: Callable[[SessionTransport, SessionSpec], Any],
) -> Callable[..., ExecutorBackend]:
    def _factory(
        kind: str,
        policy: ToolPolicy,
        coordinator: HITLCoordinator,
        session_id: str,
        **kwargs: object,
    ) -> ExecutorBackend:
        del kind, policy, coordinator, session_id, kwargs
        return InProcessExecutor(runner=runner)

    return _factory


# ── fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def store_engine(tmp_path):
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/_store.db")
    await create_all_tables(eng)
    yield eng
    await eng.dispose()


def _make_manager(
    store_engine,
    tmp_path: Path,
    *,
    runner: Callable[[SessionTransport, SessionSpec], Any],
    max_concurrent: int = 4,
    paused_timeout_s: int = 60,
    max_paused: int = 50,
    max_paused_per_api_key: int = 20,
    resume_timeout_s: float = 5.0,
) -> SessionManager:
    bus = EventBus()
    coord = HITLCoordinator()
    redactor = RedactionEngine()
    return SessionManager(
        executor_factory=_make_factory(runner),
        assembler=_NoopAssembler(),
        store=SessionRepository(store_engine),
        bus=bus,
        coordinator=coord,
        redactor=redactor,
        default_policy=ToolPolicy(),
        install_dir_root=tmp_path / "installs",
        default_timeout_s=10,
        max_concurrent=max_concurrent,
        grace_period_s=1,
        paused_timeout_s=paused_timeout_s,
        max_paused=max_paused,
        max_paused_per_api_key=max_paused_per_api_key,
        resume_timeout_s=resume_timeout_s,
    )


def _spec(tmp_path: Path) -> SessionSpec:
    return SessionSpec(
        prompt="hi",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
        timeout_s=10,
    )


async def _wait_running(
    manager: SessionManager, sid: str, *, timeout: float = 2.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        det = await manager.get(sid)
        if det.status == SessionState.RUNNING:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"session {sid} never reached RUNNING; last={det.status}")
        await asyncio.sleep(0.01)


async def _wait_status(
    manager: SessionManager,
    sid: str,
    target: SessionState,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        det = await manager.get(sid)
        if det.status == target:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"session {sid} never reached {target}; last={det.status}"
            )
        await asyncio.sleep(0.01)


def _install_bridge(manager: SessionManager, sid: str, bridge: _MockBridge) -> None:
    """Stash the mock in the manager's per-session bridge map. We use
    ``cast`` because :class:`_MockBridge` only structurally implements
    the pause/resume protocol — mypy would otherwise reject this."""
    manager._bridges[sid] = cast(InProcessBridge, bridge)


# ── tests ─────────────────────────────────────────────────────────────


class TestPauseTransitions:
    async def test_pause_running_to_paused_emits_event(
        self, store_engine, tmp_path: Path
    ):
        runner = _BlockingRunner()
        manager = _make_manager(store_engine, tmp_path, runner=runner)
        events: list[SessionStateChanged] = []

        async def _drain() -> None:
            async for ev in manager._bus.subscribe(SessionStateChanged):
                events.append(ev)

        drainer = asyncio.create_task(_drain())
        try:
            sid = await manager.submit(_spec(tmp_path))
            await _wait_running(manager, sid)
            bridge = _MockBridge()
            _install_bridge(manager, sid, bridge)

            await manager.pause(sid, reason="hitl_wait")
            # Yield so the drain coroutine catches up with the publish.
            await asyncio.sleep(0.05)

            det = await manager.get(sid)
            assert det.status == SessionState.PAUSED
            assert bridge.pause_calls == ["hitl_wait"]
            paused_events = [
                e
                for e in events
                if e.to_state == SessionState.PAUSED.value
            ]
            assert len(paused_events) == 1
            assert paused_events[0].from_state == SessionState.RUNNING.value
            assert paused_events[0].reason == "hitl_wait"

            # IMPORTANT ordering: call resume() BEFORE releasing the runner.
            # The blocking runner doesn't actually honour our advisory pause
            # (the mock bridge no-ops). If we released the event first, the
            # runner would race to send session.end while resume() was still
            # in its await chain and the manager's final state-write order
            # would be non-deterministic. Resuming first lets the manager
            # transition back to RUNNING cleanly before the runner finishes.
            await manager.resume(sid)
            runner.released.set()
            await _wait_status(manager, sid, SessionState.COMPLETED, timeout=2.0)
        finally:
            drainer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drainer

    async def test_pause_releases_semaphore_slot(
        self, store_engine, tmp_path: Path
    ):
        runner = _BlockingRunner()
        manager = _make_manager(store_engine, tmp_path, runner=runner, max_concurrent=1)
        sid = await manager.submit(_spec(tmp_path))
        await _wait_running(manager, sid)
        bridge = _MockBridge()
        _install_bridge(manager, sid, bridge)

        slots_before = manager._sem._value
        await manager.pause(sid)
        slots_after = manager._sem._value

        # Pause releases exactly one slot.
        assert slots_after == slots_before + 1
        assert sid in manager._paused_holds_slot

        # cleanup
        await manager.resume(sid)
        runner.released.set()
        await _wait_status(manager, sid, SessionState.COMPLETED, timeout=2.0)

    async def test_resume_paused_to_running_emits_event(
        self, store_engine, tmp_path: Path
    ):
        runner = _BlockingRunner()
        manager = _make_manager(store_engine, tmp_path, runner=runner)
        events: list[SessionStateChanged] = []

        async def _drain() -> None:
            async for ev in manager._bus.subscribe(SessionStateChanged):
                events.append(ev)

        drainer = asyncio.create_task(_drain())
        try:
            sid = await manager.submit(_spec(tmp_path))
            await _wait_running(manager, sid)
            bridge = _MockBridge()
            _install_bridge(manager, sid, bridge)
            await manager.pause(sid)

            await manager.resume(sid, hint="continue-please")
            await asyncio.sleep(0.05)

            det = await manager.get(sid)
            assert det.status == SessionState.RUNNING
            assert bridge.resume_calls == ["continue-please"]
            resume_events = [
                e
                for e in events
                if e.from_state == SessionState.PAUSED.value
                and e.to_state == SessionState.RUNNING.value
            ]
            assert len(resume_events) == 1
            assert resume_events[0].reason == "continue-please"

            runner.released.set()
            await _wait_status(manager, sid, SessionState.COMPLETED, timeout=2.0)
        finally:
            drainer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drainer

    async def test_double_pause_is_idempotent(
        self, store_engine, tmp_path: Path
    ):
        runner = _BlockingRunner()
        manager = _make_manager(store_engine, tmp_path, runner=runner)
        sid = await manager.submit(_spec(tmp_path))
        await _wait_running(manager, sid)
        bridge = _MockBridge()
        _install_bridge(manager, sid, bridge)

        await manager.pause(sid, reason="first")
        await manager.pause(sid, reason="second")  # no-op except timer re-arm

        # Bridge was only called once — second pause short-circuits.
        assert len(bridge.pause_calls) == 1
        det = await manager.get(sid)
        assert det.status == SessionState.PAUSED

        await manager.resume(sid)
        runner.released.set()
        await _wait_status(manager, sid, SessionState.COMPLETED, timeout=2.0)


class TestErrorMapping:
    async def test_pause_unknown_session_raises_not_found(
        self, store_engine, tmp_path: Path
    ):
        runner = _BlockingRunner()
        manager = _make_manager(store_engine, tmp_path, runner=runner)
        with pytest.raises(SessionNotFound):
            await manager.pause("does-not-exist")

    async def test_pause_already_completed_raises_not_running(
        self, store_engine, tmp_path: Path
    ):
        runner = _BlockingRunner()
        runner.released.set()  # let session complete immediately
        manager = _make_manager(store_engine, tmp_path, runner=runner)
        sid = await manager.submit(_spec(tmp_path))
        await _wait_status(manager, sid, SessionState.COMPLETED, timeout=2.0)
        # Bridge has been cleaned up by _run's finally — pause should
        # see the row in COMPLETED state and raise SessionNotRunning.
        with pytest.raises(SessionNotRunning):
            await manager.pause(sid)

    async def test_resume_unknown_session_raises_not_found(
        self, store_engine, tmp_path: Path
    ):
        runner = _BlockingRunner()
        manager = _make_manager(store_engine, tmp_path, runner=runner)
        with pytest.raises(SessionNotFound):
            await manager.resume("does-not-exist")

    async def test_resume_running_session_raises_not_paused(
        self, store_engine, tmp_path: Path
    ):
        runner = _BlockingRunner()
        manager = _make_manager(store_engine, tmp_path, runner=runner)
        sid = await manager.submit(_spec(tmp_path))
        await _wait_running(manager, sid)
        with pytest.raises(SessionNotPaused):
            await manager.resume(sid)
        runner.released.set()
        # Session naturally completes after release.
        await _wait_status(manager, sid, SessionState.COMPLETED, timeout=2.0)


class TestPausedTimeout:
    async def test_paused_timeout_cancels_session(
        self, store_engine, tmp_path: Path
    ):
        runner = _BlockingRunner()
        manager = _make_manager(
            store_engine, tmp_path, runner=runner, paused_timeout_s=0
        )
        sid = await manager.submit(_spec(tmp_path))
        await _wait_running(manager, sid)
        bridge = _MockBridge()
        _install_bridge(manager, sid, bridge)
        await manager.pause(sid)

        # The watchdog should cancel within a few ticks.
        await _wait_status(manager, sid, SessionState.CANCELLED, timeout=2.0)
        det = await manager.get(sid)
        assert det.end_reason == "cancelled"


class TestResumeTimeout:
    async def test_resume_times_out_when_semaphore_blocked(
        self, store_engine, tmp_path: Path
    ):
        # max_concurrent=1 + a second session occupying the slot ⇒ resume
        # cannot reacquire within the timeout.
        runner_a = _BlockingRunner()
        runner_b = _BlockingRunner()

        bus = EventBus()
        coord = HITLCoordinator()

        # Mixed: factory returns runner_a for the first call, runner_b second.
        call_count = {"n": 0}

        def _factory(
            kind: str,
            policy: ToolPolicy,
            coordinator: HITLCoordinator,
            session_id: str,
            **kwargs: object,
        ) -> ExecutorBackend:
            del kind, policy, coordinator, session_id, kwargs
            call_count["n"] += 1
            runner = runner_a if call_count["n"] == 1 else runner_b
            return InProcessExecutor(runner=runner)

        manager = SessionManager(
            executor_factory=_factory,
            assembler=_NoopAssembler(),
            store=SessionRepository(store_engine),
            bus=bus,
            coordinator=coord,
            redactor=RedactionEngine(),
            default_policy=ToolPolicy(),
            install_dir_root=tmp_path / "installs",
            default_timeout_s=10,
            max_concurrent=1,
            grace_period_s=1,
            resume_timeout_s=0.2,
        )

        sid_a = await manager.submit(_spec(tmp_path))
        await _wait_running(manager, sid_a)
        bridge = _MockBridge()
        _install_bridge(manager, sid_a, bridge)

        await manager.pause(sid_a)
        # Slot is now free. Submit second session which immediately grabs it.
        sid_b = await manager.submit(_spec(tmp_path))
        await _wait_running(manager, sid_b)

        # Resume of A should fail because B holds the only slot.
        with pytest.raises(ResumeQueueTimeout):
            await manager.resume(sid_a)

        # A is still in PAUSED state after the failed resume.
        det = await manager.get(sid_a)
        assert det.status == SessionState.PAUSED

        # cleanup — release both blocking runners. After A is released the
        # runner naturally sends session.end → A settles to COMPLETED even
        # though we believed it was paused (the mock bridge is a no-op so
        # the runner ignored our advisory pause). This is fine for the
        # test's purpose: we only needed to verify resume() raised.
        runner_a.released.set()
        runner_b.released.set()
        await _wait_status(manager, sid_b, SessionState.COMPLETED, timeout=2.0)
        for _ in range(50):
            det = await manager.get(sid_a)
            if det.status in {SessionState.COMPLETED, SessionState.CANCELLED}:
                break
            await asyncio.sleep(0.02)
        det_a = await manager.get(sid_a)
        assert det_a.status in {SessionState.COMPLETED, SessionState.CANCELLED}


class TestShutdownCoord:
    async def test_shutdown_cancels_paused_with_reason(
        self, store_engine, tmp_path: Path
    ):
        runner = _BlockingRunner()
        manager = _make_manager(store_engine, tmp_path, runner=runner)
        sid = await manager.submit(_spec(tmp_path))
        await _wait_running(manager, sid)
        bridge = _MockBridge()
        _install_bridge(manager, sid, bridge)
        await manager.pause(sid)

        # Default paused_action='cancel' should settle paused row.
        await manager.shutdown(grace_period_s=0)

        det = await manager.get(sid)
        assert det.status == SessionState.CANCELLED
        assert det.end_reason == "cancelled"
