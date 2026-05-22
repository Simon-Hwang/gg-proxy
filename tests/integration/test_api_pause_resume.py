"""FastAPI /api/v1/sessions pause/resume/DELETE integration tests — Plan 6 Task 4.

These tests run against the real FastAPI app via :class:`httpx.AsyncClient`
+ :class:`ASGITransport`, so middleware, lifespan, and dependency injection
are all exercised. The executor factory is overridden to return an
:class:`InProcessExecutor` driven by a blocking runner so the session stays
in ``RUNNING`` long enough for the test to issue pause/resume.

Pause/resume bridges are mocked by injecting a :class:`_MockBridge`
directly into ``manager._bridges[sid]`` once the session is observed in
RUNNING. This bypasses the wire control loop but exercises every
SessionManager + route + HTTP error-mapping code path.
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

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.session.control import ControlAck
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.executor.protocol import ExecutorBackend
from gg_relay.session.frames import make_msg_chunk, make_session_end
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.manager import SessionManager
from gg_relay.session.runner.bridge import BridgeAckTimeout
from gg_relay.session.runner.inprocess_control import InProcessBridge
from gg_relay.session.spec import SessionSpec
from gg_relay.session.transport.protocol import SessionTransport

pytestmark = pytest.mark.asyncio

HEADERS = {"X-API-Key": "k1"}


@dataclass
class _BlockingRunner:
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
class _MockBridge:
    pause_calls: list[str | None] = field(default_factory=list)
    resume_calls: list[str | None] = field(default_factory=list)
    pause_raises: BaseException | None = None
    resume_raises: BaseException | None = None
    pause_ok: bool = True
    resume_ok: bool = True

    async def pause(self, *, reason: str | None = None) -> ControlAck:
        self.pause_calls.append(reason)
        if self.pause_raises is not None:
            raise self.pause_raises
        return ControlAck(
            op="pause", req_id=f"p-{len(self.pause_calls)}", ok=self.pause_ok
        )

    async def resume(self, *, hint: str | None = None) -> ControlAck:
        self.resume_calls.append(hint)
        if self.resume_raises is not None:
            raise self.resume_raises
        return ControlAck(
            op="resume", req_id=f"r-{len(self.resume_calls)}", ok=self.resume_ok
        )


# Per-test factory state so we can hand out a fresh blocking runner per
# submitted session and release them in cleanup.
@dataclass
class _State:
    runners: list[_BlockingRunner] = field(default_factory=list)


def _make_factory(
    state: _State,
) -> Callable[..., ExecutorBackend]:
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
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/api.db"
    cfg.api_keys_raw = "k1,k2"
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://localhost:8000"
    cfg.default_timeout_s = 10
    cfg.grace_period_s = 1
    cfg.max_concurrent_sessions = 2
    cfg.max_paused = 5
    cfg.max_paused_per_api_key = 2
    cfg.paused_timeout_s = 60
    cfg.resume_timeout_s = 0.5
    return cfg


@pytest_asyncio.fixture
async def state() -> _State:
    return _State()


@pytest_asyncio.fixture
async def client(
    tmp_path: Path, state: _State
) -> AsyncIterator[tuple[AsyncClient, SessionManager]]:
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
        yield ac, manager
        # Release any held runners so the lifespan teardown is clean.
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
    *,
    api_key: str = "k1",
) -> str:
    r = await ac.post(
        "/api/v1/sessions",
        json=_spec_body(tmp_path),
        headers={"X-API-Key": api_key},
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
    manager: SessionManager, sid: str, bridge: _MockBridge
) -> None:
    manager._bridges[sid] = cast(InProcessBridge, bridge)


# ── tests ─────────────────────────────────────────────────────────────


class TestPauseEndpoint:
    async def test_pause_returns_202(
        self, client: tuple[AsyncClient, SessionManager], tmp_path: Path
    ):
        ac, manager = client
        sid = await _submit_and_wait_running(ac, manager, tmp_path)
        _install_bridge(manager, sid, _MockBridge())

        r = await ac.post(
            f"/api/v1/sessions/{sid}/pause",
            json={"reason": "hitl_wait"},
            headers=HEADERS,
        )
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "paused"
        assert body["reason"] == "hitl_wait"

        det = await manager.get(sid)
        assert det.status.value == "paused"

    async def test_pause_empty_body_works(
        self, client: tuple[AsyncClient, SessionManager], tmp_path: Path
    ):
        ac, manager = client
        sid = await _submit_and_wait_running(ac, manager, tmp_path)
        _install_bridge(manager, sid, _MockBridge())
        r = await ac.post(f"/api/v1/sessions/{sid}/pause", headers=HEADERS)
        assert r.status_code == 202

    async def test_pause_unknown_returns_404(
        self, client: tuple[AsyncClient, SessionManager]
    ):
        ac, _ = client
        r = await ac.post(
            "/api/v1/sessions/does-not-exist/pause", headers=HEADERS
        )
        assert r.status_code == 404

    async def test_pause_already_completed_returns_409(
        self, client: tuple[AsyncClient, SessionManager], tmp_path: Path
    ):
        ac, manager = client
        sid = await _submit_and_wait_running(ac, manager, tmp_path)
        # Don't install a bridge; release the runner so the session ends.
        for runner in [r for r in [_BlockingRunner()] if r]:
            del runner  # no-op silencer
        # The runner was created inside the factory — release via state.
        # The test fixture has access to state.runners via the manager
        # but we need direct access. Workaround: just call cancel via
        # DELETE then poll status==cancelled.
        await ac.delete(f"/api/v1/sessions/{sid}", headers=HEADERS)
        deadline = asyncio.get_running_loop().time() + 2.0
        while True:
            det = await manager.get(sid)
            if det.status.value in {"cancelled", "completed"}:
                break
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.01)
        # Now pause should 409.
        r = await ac.post(f"/api/v1/sessions/{sid}/pause", headers=HEADERS)
        assert r.status_code == 409

    async def test_pause_max_paused_returns_429_with_retry_after(
        self,
        client: tuple[AsyncClient, SessionManager],
        tmp_path: Path,
        state: _State,
    ):
        ac, manager = client
        # max_paused_per_api_key=2 from cfg. We need to submit, pause, then
        # repeat — pausing immediately frees the semaphore slot so the next
        # submit's session can reach RUNNING without waiting (avoids the
        # max_concurrent_sessions=2 throttle for the third submit).
        sids: list[str] = []
        for _ in range(3):
            sid = await _submit_and_wait_running(ac, manager, tmp_path)
            _install_bridge(manager, sid, _MockBridge())
            sids.append(sid)
            if len(sids) < 3:
                r = await ac.post(
                    f"/api/v1/sessions/{sid}/pause", headers=HEADERS
                )
                assert r.status_code == 202
        # Third pause — per-api-key cap (2) already reached.
        r = await ac.post(f"/api/v1/sessions/{sids[2]}/pause", headers=HEADERS)
        assert r.status_code == 429
        assert "Retry-After" in r.headers
        body = r.json()
        assert body["code"] == "max_paused_exceeded"

    async def test_pause_bridge_timeout_returns_504(
        self, client: tuple[AsyncClient, SessionManager], tmp_path: Path
    ):
        ac, manager = client
        sid = await _submit_and_wait_running(ac, manager, tmp_path)
        bridge = _MockBridge(
            pause_raises=BridgeAckTimeout("pause ack timed out")
        )
        _install_bridge(manager, sid, bridge)
        r = await ac.post(f"/api/v1/sessions/{sid}/pause", headers=HEADERS)
        assert r.status_code == 504


class TestResumeEndpoint:
    async def test_resume_returns_202(
        self, client: tuple[AsyncClient, SessionManager], tmp_path: Path
    ):
        ac, manager = client
        sid = await _submit_and_wait_running(ac, manager, tmp_path)
        bridge = _MockBridge()
        _install_bridge(manager, sid, bridge)
        await ac.post(f"/api/v1/sessions/{sid}/pause", headers=HEADERS)

        r = await ac.post(
            f"/api/v1/sessions/{sid}/resume",
            json={"hint": "carry on"},
            headers=HEADERS,
        )
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "running"
        assert body["hint"] == "carry on"

    async def test_resume_unknown_returns_404(
        self, client: tuple[AsyncClient, SessionManager]
    ):
        ac, _ = client
        r = await ac.post(
            "/api/v1/sessions/does-not-exist/resume", headers=HEADERS
        )
        assert r.status_code == 404

    async def test_resume_not_paused_returns_409(
        self, client: tuple[AsyncClient, SessionManager], tmp_path: Path
    ):
        ac, manager = client
        sid = await _submit_and_wait_running(ac, manager, tmp_path)
        r = await ac.post(f"/api/v1/sessions/{sid}/resume", headers=HEADERS)
        assert r.status_code == 409


class TestDeleteEndpoint:
    async def test_delete_returns_202(
        self, client: tuple[AsyncClient, SessionManager], tmp_path: Path
    ):
        ac, manager = client
        sid = await _submit_and_wait_running(ac, manager, tmp_path)
        r = await ac.delete(f"/api/v1/sessions/{sid}", headers=HEADERS)
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "cancelled"
        assert body["session_id"] == sid

    async def test_delete_unknown_still_202(
        self, client: tuple[AsyncClient, SessionManager]
    ):
        ac, _ = client
        # D6.9=A: idempotent — unknown id is NOT 404.
        r = await ac.delete(
            "/api/v1/sessions/does-not-exist", headers=HEADERS
        )
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "cancelled"

    async def test_delete_already_cancelled_still_202(
        self, client: tuple[AsyncClient, SessionManager], tmp_path: Path
    ):
        ac, manager = client
        sid = await _submit_and_wait_running(ac, manager, tmp_path)
        await ac.delete(f"/api/v1/sessions/{sid}", headers=HEADERS)
        # Second call should also be 202.
        r = await ac.delete(f"/api/v1/sessions/{sid}", headers=HEADERS)
        assert r.status_code == 202
