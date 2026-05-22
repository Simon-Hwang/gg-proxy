"""SessionManager max_paused / per-api-key cap tests — Plan 6 Task 3 + D6.17.

These tests use the same `_MockBridge` + `_BlockingRunner` plumbing as
``test_pause_resume.py`` to drive sessions into a paused state without
touching the real wire/SDK control loop.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio

from gg_relay.core import EventBus, SessionState
from gg_relay.redaction import RedactionEngine
from gg_relay.session.control import ControlAck
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.executor.protocol import ExecutorBackend
from gg_relay.session.frames import make_msg_chunk, make_session_end
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.manager import MaxPausedExceeded, SessionManager
from gg_relay.session.runner.inprocess_control import InProcessBridge
from gg_relay.session.spec import PluginManifest, SessionSpec
from gg_relay.session.transport.protocol import SessionTransport
from gg_relay.store import SessionRepository, create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


class _NoopAssembler:
    async def prepare(self, spec: SessionSpec, *, install_dir: Path) -> None:
        del spec, install_dir
        return None


@dataclass
class _BlockingRunner:
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


class _MockBridge:
    """Always-ack mock matching pause/resume interface."""

    def __init__(self) -> None:
        self._seq = 0

    async def pause(self, *, reason: str | None = None) -> ControlAck:
        del reason
        self._seq += 1
        return ControlAck(op="pause", req_id=f"pause-{self._seq}", ok=True)

    async def resume(self, *, hint: str | None = None) -> ControlAck:
        del hint
        self._seq += 1
        return ControlAck(op="resume", req_id=f"resume-{self._seq}", ok=True)


# Each session gets its own runner via factory_state.
@dataclass
class _FactoryState:
    runners: list[_BlockingRunner] = field(default_factory=list)

    def factory(
        self,
        kind: str,
        policy: ToolPolicy,
        coordinator: HITLCoordinator,
        session_id: str,
        **kwargs: object,
    ) -> ExecutorBackend:
        del kind, policy, coordinator, session_id, kwargs
        runner = _BlockingRunner()
        self.runners.append(runner)
        return InProcessExecutor(runner=runner)


@pytest_asyncio.fixture
async def store_engine(tmp_path):
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/_store.db")
    await create_all_tables(eng)
    yield eng
    await eng.dispose()


def _spec(tmp_path: Path) -> SessionSpec:
    return SessionSpec(
        prompt="hi",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
        timeout_s=10,
    )


def _make_manager(
    store_engine,
    tmp_path: Path,
    *,
    factory_state: _FactoryState,
    max_paused: int = 50,
    max_paused_per_api_key: int = 20,
    max_concurrent: int = 10,
) -> SessionManager:
    return SessionManager(
        executor_factory=factory_state.factory,
        assembler=_NoopAssembler(),
        store=SessionRepository(store_engine),
        bus=EventBus(),
        coordinator=HITLCoordinator(),
        redactor=RedactionEngine(),
        default_policy=ToolPolicy(),
        install_dir_root=tmp_path / "installs",
        default_timeout_s=10,
        max_concurrent=max_concurrent,
        grace_period_s=1,
        paused_timeout_s=60,
        max_paused=max_paused,
        max_paused_per_api_key=max_paused_per_api_key,
        resume_timeout_s=5.0,
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
            raise AssertionError(f"{sid} never reached RUNNING; last={det.status}")
        await asyncio.sleep(0.01)


async def _submit_paused(
    manager: SessionManager,
    tmp_path: Path,
    *,
    api_key_id: str | None = None,
) -> str:
    sid = await manager.submit(_spec(tmp_path), api_key_id=api_key_id)
    await _wait_running(manager, sid)
    manager._bridges[sid] = cast(InProcessBridge, _MockBridge())
    await manager.pause(sid)
    return sid


# ── tests ─────────────────────────────────────────────────────────────


async def test_global_cap_blocks_third_pause(store_engine, tmp_path: Path):
    state = _FactoryState()
    manager = _make_manager(store_engine, tmp_path, factory_state=state, max_paused=2)

    sid1 = await _submit_paused(manager, tmp_path)
    sid2 = await _submit_paused(manager, tmp_path)

    # Third submit succeeds, but pause() should be rejected.
    sid3 = await manager.submit(_spec(tmp_path))
    await _wait_running(manager, sid3)
    manager._bridges[sid3] = cast(InProcessBridge, _MockBridge())

    with pytest.raises(MaxPausedExceeded):
        await manager.pause(sid3)

    # sid3 stays RUNNING (not paused).
    det = await manager.get(sid3)
    assert det.status == SessionState.RUNNING

    # cleanup — resume the two paused sessions first so they're back to
    # RUNNING, then release the blocking runners so all three can settle.
    await manager.resume(sid1)
    await manager.resume(sid2)
    for runner in state.runners:
        runner.released.set()
    await manager.shutdown(grace_period_s=2)


async def test_per_api_key_cap_blocks(store_engine, tmp_path: Path):
    state = _FactoryState()
    manager = _make_manager(
        store_engine,
        tmp_path,
        factory_state=state,
        max_paused_per_api_key=2,
    )

    sid1 = await _submit_paused(manager, tmp_path, api_key_id="alice")
    sid2 = await _submit_paused(manager, tmp_path, api_key_id="alice")

    sid3 = await manager.submit(_spec(tmp_path), api_key_id="alice")
    await _wait_running(manager, sid3)
    manager._bridges[sid3] = cast(InProcessBridge, _MockBridge())
    with pytest.raises(MaxPausedExceeded):
        await manager.pause(sid3)

    # Different API key is unaffected.
    sid4 = await manager.submit(_spec(tmp_path), api_key_id="bob")
    await _wait_running(manager, sid4)
    manager._bridges[sid4] = cast(InProcessBridge, _MockBridge())
    await manager.pause(sid4)
    assert (await manager.get(sid4)).status == SessionState.PAUSED

    # cleanup
    for sid in (sid1, sid2, sid4):
        await manager.resume(sid)
    for runner in state.runners:
        runner.released.set()
    await manager.shutdown(grace_period_s=2)


async def test_resume_frees_a_pause_slot(store_engine, tmp_path: Path):
    state = _FactoryState()
    manager = _make_manager(
        store_engine,
        tmp_path,
        factory_state=state,
        max_paused=1,
    )

    sid1 = await _submit_paused(manager, tmp_path)

    # Second pause is blocked.
    sid2 = await manager.submit(_spec(tmp_path))
    await _wait_running(manager, sid2)
    manager._bridges[sid2] = cast(InProcessBridge, _MockBridge())
    with pytest.raises(MaxPausedExceeded):
        await manager.pause(sid2)

    # Resume sid1 → cap is free → pausing sid2 now succeeds.
    await manager.resume(sid1)
    await manager.pause(sid2)
    assert (await manager.get(sid2)).status == SessionState.PAUSED

    # cleanup
    await manager.resume(sid2)
    for runner in state.runners:
        runner.released.set()
    await manager.shutdown(grace_period_s=2)


async def test_cancel_paused_frees_pause_slot(store_engine, tmp_path: Path):
    state = _FactoryState()
    manager = _make_manager(
        store_engine,
        tmp_path,
        factory_state=state,
        max_paused=1,
    )

    sid1 = await _submit_paused(manager, tmp_path, api_key_id="alice")
    assert manager._paused_by_key.get("alice") == 1

    # Cancelling paused sid1 should decrement caps and free the slot.
    state.runners[0].released.set()
    await manager.cancel(sid1)
    deadline = asyncio.get_running_loop().time() + 2.0
    while True:
        det = await manager.get(sid1)
        if det.status == SessionState.CANCELLED:
            break
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0.01)
    assert "alice" not in manager._paused_by_key
    assert len(manager._paused_set) == 0
