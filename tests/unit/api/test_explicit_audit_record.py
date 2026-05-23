"""SessionManager explicit audit hooks — Plan 8 D8.4 / Task 5.

Verifies that the manager emits an explicit, well-typed audit row
for every sensitive mutation it owns:

* ``test_session_create_writes_audit_session_create`` — submit() →
  one row with ``action='session_create'``,
  ``target_type='session'``, ``target_id=<sid>``, and ``actor`` =
  the owner label resolved at submit.
* ``test_session_cancel_writes_audit_session_cancel`` — cancel() →
  one row with ``action='session_cancel'`` and the cancel reason in
  metadata.
* ``test_session_pause_writes_audit_session_pause`` — pause() →
  one row with ``action='session_pause'``.

The fakes mirror the pattern in ``test_audit_middleware_fallback.py``
(in-memory recording store) so the assertions stay focused on the
audit hook contract rather than the SQLAlchemy persistence layer
exercised by ``test_audit_repository.py``.

Pause/resume need a running session + a mock bridge so the existing
fixture pattern from ``test_pause_resume.py`` is reused.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio

from gg_relay.api.audit_service import AuditService
from gg_relay.core import EventBus, SessionState
from gg_relay.redaction import RedactionEngine
from gg_relay.session.control import ControlAck
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.executor.protocol import ExecutorBackend
from gg_relay.session.frames import make_msg_chunk, make_session_end
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.manager import SessionManager
from gg_relay.session.runner.inprocess_control import InProcessBridge
from gg_relay.session.spec import PluginManifest, SessionSpec
from gg_relay.session.transport.protocol import SessionTransport
from gg_relay.store import SessionRepository, create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


# ── fakes (mirroring tests/unit/session/test_pause_resume.py) ─────────


class _NoopAssembler:
    async def prepare(self, spec: SessionSpec, *, install_dir: Path) -> None:
        del spec, install_dir
        return None


@dataclass
class _BlockingRunner:
    """Runner that blocks until released — keeps the session in RUNNING."""

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
    pause_calls: list[str | None] = field(default_factory=list)
    resume_calls: list[str | None] = field(default_factory=list)

    async def pause(self, *, reason: str | None = None) -> ControlAck:
        self.pause_calls.append(reason)
        return ControlAck(
            op="pause", req_id=f"pause-{len(self.pause_calls)}", ok=True
        )

    async def resume(self, *, hint: str | None = None) -> ControlAck:
        self.resume_calls.append(hint)
        return ControlAck(
            op="resume", req_id=f"resume-{len(self.resume_calls)}", ok=True
        )


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


class _RecordingStore:
    """In-memory AuditStore-like that captures every record call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def record_audit(
        self,
        *,
        actor: str,
        action: str,
        target_type: str | None = None,
        target_id: str | None = None,
        metadata: Any = None,
        request_id: str | None = None,
        ts: Any = None,
        conn: Any = None,
    ) -> int:
        self.calls.append(
            {
                "actor": actor,
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "metadata": dict(metadata) if metadata else None,
                "request_id": request_id,
            }
        )
        return len(self.calls)


# ── fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def store_engine(tmp_path: Path):
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/_store.db")
    await create_all_tables(eng)
    yield eng
    await eng.dispose()


def _make_manager(
    store_engine,
    tmp_path: Path,
    *,
    runner: Callable[[SessionTransport, SessionSpec], Any],
    audit_service: AuditService,
) -> SessionManager:
    return SessionManager(
        executor_factory=_make_factory(runner),
        assembler=_NoopAssembler(),
        store=SessionRepository(store_engine),
        bus=EventBus(),
        coordinator=HITLCoordinator(),
        redactor=RedactionEngine(),
        default_policy=ToolPolicy(),
        install_dir_root=tmp_path / "installs",
        default_timeout_s=10,
        max_concurrent=4,
        grace_period_s=1,
        audit_service=audit_service,
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
            raise AssertionError(
                f"session {sid} never reached RUNNING; last={det.status}"
            )
        await asyncio.sleep(0.01)


def _install_bridge(
    manager: SessionManager, sid: str, bridge: _MockBridge
) -> None:
    manager._bridges[sid] = cast(InProcessBridge, bridge)


# ── tests ─────────────────────────────────────────────────────────────


async def test_session_create_writes_audit_session_create(
    store_engine, tmp_path: Path
) -> None:
    """submit() → one ``action='session_create'`` row with the right shape."""
    recorder = _RecordingStore()
    audit = AuditService(recorder)
    manager = _make_manager(
        store_engine,
        tmp_path,
        runner=_BlockingRunner(),
        audit_service=audit,
    )
    try:
        sid = await manager.submit(_spec(tmp_path), owner="alice")
        # The submit hook is synchronous w.r.t. the manager — exactly
        # one audit call should already be recorded.
        creates = [c for c in recorder.calls if c["action"] == "session_create"]
        assert len(creates) == 1, (
            f"expected exactly 1 session_create row, got {recorder.calls!r}"
        )
        c = creates[0]
        assert c["actor"] == "alice"
        assert c["target_type"] == "session"
        assert c["target_id"] == sid
        assert c["metadata"] is not None
        assert c["metadata"]["backend"] == "inprocess"
        assert c["metadata"]["tags"] == []
    finally:
        await manager.shutdown(grace_period_s=0)


async def test_session_cancel_writes_audit_session_cancel(
    store_engine, tmp_path: Path
) -> None:
    """cancel() → one ``action='session_cancel'`` row."""
    recorder = _RecordingStore()
    audit = AuditService(recorder)
    manager = _make_manager(
        store_engine,
        tmp_path,
        runner=_BlockingRunner(),
        audit_service=audit,
    )
    try:
        sid = await manager.submit(_spec(tmp_path), owner="bob")
        recorder.calls.clear()  # drop the create row to focus on cancel
        await manager.cancel(sid, reason="user_clicked_x")
        # cancel writes its row before tearing down the task; we also
        # tolerate the background task emitting nothing additional.
        cancels = [c for c in recorder.calls if c["action"] == "session_cancel"]
        assert len(cancels) == 1, (
            f"expected exactly 1 session_cancel row, got {recorder.calls!r}"
        )
        c = cancels[0]
        assert c["actor"] == "bob"
        assert c["target_type"] == "session"
        assert c["target_id"] == sid
        assert c["metadata"] == {"reason": "user_clicked_x"}
    finally:
        await manager.shutdown(grace_period_s=0)


async def test_session_pause_writes_audit_session_pause(
    store_engine, tmp_path: Path
) -> None:
    """pause() → one ``action='session_pause'`` row."""
    recorder = _RecordingStore()
    audit = AuditService(recorder)
    runner = _BlockingRunner()
    manager = _make_manager(
        store_engine, tmp_path, runner=runner, audit_service=audit
    )
    try:
        sid = await manager.submit(_spec(tmp_path), owner="carol")
        await _wait_running(manager, sid)
        bridge = _MockBridge()
        _install_bridge(manager, sid, bridge)

        recorder.calls.clear()  # focus on pause
        await manager.pause(sid, reason="awaiting_input")

        pauses = [c for c in recorder.calls if c["action"] == "session_pause"]
        assert len(pauses) == 1, (
            f"expected exactly 1 session_pause row, got {recorder.calls!r}"
        )
        c = pauses[0]
        assert c["actor"] == "carol"
        assert c["target_type"] == "session"
        assert c["target_id"] == sid
        assert c["metadata"] == {"reason": "awaiting_input"}

        # Release the runner so the session ends cleanly during shutdown.
        runner.released.set()
    finally:
        await manager.shutdown(grace_period_s=2)
