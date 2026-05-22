"""Unit tests for :class:`DockerExecutor` with a mocked ``aiodocker.Docker``.

The real docker round-trip lives in
``tests/integration/test_docker_executor.py`` behind
``@requires_docker @requires_api_key``; here we focus on the wiring the
executor itself owns: config dict shape, env composition, accept timing,
graceful stop, health probe.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gg_relay.session.executor.docker import DockerExecutor, _parse_size
from gg_relay.session.spec import (
    PluginManifest,
    SessionRuntimeContext,
    SessionSpec,
)
from gg_relay.session.transport.unixsocket import UnixSocketTransport


def _spec(tmp_path: Path) -> SessionSpec:
    return SessionSpec(
        prompt="hi",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal", extra_env=(("X", "1"),)),
    )


def _runtime_ctx() -> SessionRuntimeContext:
    return SessionRuntimeContext(
        credentials={"ANTHROPIC_API_KEY": "sk-fake-abc"},
        trace_id="trace-7",
        public_callback_base="https://relay.example.com",
    )


def _mock_docker() -> tuple[MagicMock, MagicMock]:
    """Return (docker_client_mock, container_mock) with the right async-ness."""
    container = MagicMock()
    container.id = "abc123"
    container.show = AsyncMock(return_value={"State": {"Status": "running"}})
    container.wait = AsyncMock(return_value={"StatusCode": 0})
    container.kill = AsyncMock(return_value=None)

    client = MagicMock()
    client.containers = MagicMock()
    client.containers.run = AsyncMock(return_value=container)
    client.close = AsyncMock(return_value=None)
    return client, container


@pytest.fixture
def patched_socket_root():
    """A short socket dir under /tmp.

    Cannot use ``tmp_path`` directly: pytest's path
    (``/tmp/pytest-of-root/pytest-N/test_xxx0/``) plus our ``{32-char hex}.sock``
    filename comfortably exceeds Linux's 108-byte AF_UNIX limit. Keep this
    short and clean up after the test.
    """
    import shutil
    import tempfile

    p = Path(tempfile.mkdtemp(prefix="ggrs-", dir="/tmp"))
    try:
        yield p
    finally:
        shutil.rmtree(p, ignore_errors=True)


@pytest.fixture
async def auto_accept_socket(patched_socket_root: Path):
    """Background task that opens a client connection to whatever socket file
    appears under ``patched_socket_root``. Lets DockerExecutor.start()'s
    server.accept() resolve in the mocked-docker tests."""
    stop = asyncio.Event()
    connected: list[UnixSocketTransport] = []

    async def watcher():
        while not stop.is_set():
            files = list(patched_socket_root.glob("*.sock"))
            if files:
                try:
                    t = await UnixSocketTransport.connect(
                        files[0], retry_timeout=2.0
                    )
                    connected.append(t)
                    return
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(0.02)

    import contextlib

    task = asyncio.create_task(watcher())
    yield connected
    stop.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    for t in connected:
        await t.close()


class TestParseSize:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1k", 1024),
            ("2m", 2 * 1024 * 1024),
            ("2g", 2 * 1024 * 1024 * 1024),
            ("512M", 512 * 1024 * 1024),
            ("1024", 1024),
        ],
    )
    def test_known_units(self, raw, expected):
        assert _parse_size(raw) == expected

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            _parse_size("")


class TestStartHappyPath:
    async def test_start_creates_container_and_returns_handle(
        self,
        tmp_path: Path,
        patched_socket_root: Path,
        auto_accept_socket,
    ):
        client, container = _mock_docker()
        executor = DockerExecutor(
            image="ghcr.io/test/runner:latest",
            socket_root=patched_socket_root,
            docker_client=client,
        )
        handle = await executor.start(_spec(tmp_path), runtime_ctx=_runtime_ctx())

        client.containers.run.assert_awaited_once()
        # name prefix matches Plan 3's `gg-relay-{runtime_id[:8]}` convention
        kwargs = client.containers.run.await_args.kwargs
        assert kwargs["name"].startswith("gg-relay-")
        assert handle.backend == "docker"
        assert handle.runtime_id
        assert handle.transport.is_alive

        # extra fields are surfaced
        extras = dict(handle.extra)
        assert extras["container_id"] == "abc123"
        assert extras["image"] == "ghcr.io/test/runner:latest"
        assert extras["socket_path"].startswith(str(patched_socket_root))

        await executor.stop(handle)
        # auto-accept's client transport is in `connected`; one connection was
        # established → executor wiring works end-to-end through the mock.
        assert len(auto_accept_socket) >= 1
        del container  # keep linter happy

    async def test_start_runs_container_with_expected_config(
        self,
        tmp_path: Path,
        patched_socket_root: Path,
        auto_accept_socket,
    ):
        client, _container = _mock_docker()
        executor = DockerExecutor(
            socket_root=patched_socket_root,
            docker_client=client,
            default_mem="1g",
            default_cpus=4.0,
            default_pids_limit=256,
        )
        spec = _spec(tmp_path)
        handle = await executor.start(spec, runtime_ctx=_runtime_ctx())

        cfg: dict[str, Any] = client.containers.run.await_args.kwargs["config"]
        host_cfg = cfg["HostConfig"]

        # D3.6 — resource limits
        assert host_cfg["Memory"] == 1024**3
        assert host_cfg["NanoCpus"] == int(4.0 * 1e9)
        assert host_cfg["PidsLimit"] == 256
        # D3.7 — bridge network + host-gateway addendum
        assert host_cfg["NetworkMode"] == "bridge"
        assert "host.docker.internal:host-gateway" in host_cfg["ExtraHosts"]
        # New decision — :z mount for SELinux
        binds = host_cfg["Binds"]
        assert any(":/var/run/gg-relay:rw,z" in b for b in binds)
        assert any(":/workspace:rw,z" in b for b in binds)
        # D3.16 — credentials are in env, NOT spec_json
        env_list = cfg["Env"]
        assert "ANTHROPIC_API_KEY=sk-fake-abc" in env_list
        # spec_json must NOT contain the credential
        spec_env = next(e for e in env_list if e.startswith("GG_RELAY_SPEC_JSON="))
        assert "ANTHROPIC_API_KEY" not in spec_env
        # extra_env from the SessionSpec is propagated
        assert "X=1" in env_list
        # OTel trace correlation is threaded through
        assert "RELAY_TRACE_ID=trace-7" in env_list

        await executor.stop(handle)


class TestStartFailures:
    async def test_container_run_failure_releases_socket(
        self,
        tmp_path: Path,
        patched_socket_root: Path,
    ):
        client, _container = _mock_docker()
        client.containers.run = AsyncMock(side_effect=RuntimeError("daemon down"))
        executor = DockerExecutor(
            socket_root=patched_socket_root, docker_client=client
        )
        with pytest.raises(RuntimeError, match="daemon down"):
            await executor.start(_spec(tmp_path), runtime_ctx=_runtime_ctx())
        # socket file must have been cleaned up
        assert list(patched_socket_root.glob("*.sock")) == []
        # no tracked container should remain
        assert executor._containers == {}

    async def test_accept_timeout_kills_container_and_cleans_up(
        self,
        tmp_path: Path,
        patched_socket_root: Path,
    ):
        client, container = _mock_docker()
        executor = DockerExecutor(
            socket_root=patched_socket_root,
            docker_client=client,
            accept_timeout=0.2,
        )
        # No auto_accept fixture → server.accept() will hit the 0.2s timeout.
        with pytest.raises(TimeoutError):
            await executor.start(_spec(tmp_path), runtime_ctx=_runtime_ctx())
        container.kill.assert_awaited()
        assert executor._containers == {}
        assert executor._servers == {}


class TestStopAndHealth:
    async def test_stop_sends_shutdown_then_waits_then_falls_back_to_kill(
        self,
        tmp_path: Path,
        patched_socket_root: Path,
        auto_accept_socket,
    ):
        client, container = _mock_docker()

        # Container "wait" hangs forever — exercise the kill fallback. Define
        # this as a fresh coroutine on every call so AsyncMock doesn't reuse
        # a single (and therefore non-awaitable on the second call) coroutine.
        async def _hang() -> dict[str, Any]:
            await asyncio.sleep(10)
            return {"StatusCode": 137}

        container.wait = AsyncMock(side_effect=_hang)
        executor = DockerExecutor(
            socket_root=patched_socket_root,
            docker_client=client,
            shutdown_grace_s=0.1,
        )
        handle = await executor.start(_spec(tmp_path), runtime_ctx=_runtime_ctx())
        runner_transport = auto_accept_socket[0]
        recv_task = asyncio.create_task(runner_transport.recv())

        await executor.stop(handle)
        container.kill.assert_awaited()
        # The runner side must have observed the shutdown control frame.
        frame = await asyncio.wait_for(recv_task, timeout=1.0)
        assert frame["type"] == "shutdown"

    async def test_health_running_returns_true_then_false_after_stop(
        self,
        tmp_path: Path,
        patched_socket_root: Path,
        auto_accept_socket,
    ):
        client, container = _mock_docker()
        executor = DockerExecutor(
            socket_root=patched_socket_root, docker_client=client
        )
        handle = await executor.start(_spec(tmp_path), runtime_ctx=_runtime_ctx())
        assert await executor.health(handle) is True

        # After stop(): no longer tracked → health is False.
        await executor.stop(handle)
        assert await executor.health(handle) is False
        del container

    async def test_health_returns_false_when_container_not_running(
        self,
        tmp_path: Path,
        patched_socket_root: Path,
        auto_accept_socket,
    ):
        client, container = _mock_docker()
        container.show = AsyncMock(return_value={"State": {"Status": "exited"}})
        executor = DockerExecutor(
            socket_root=patched_socket_root, docker_client=client
        )
        handle = await executor.start(_spec(tmp_path), runtime_ctx=_runtime_ctx())
        assert await executor.health(handle) is False
        await executor.stop(handle)


class TestExecutorClose:
    async def test_close_closes_owned_aiodocker_client(self):
        client, _container = _mock_docker()
        executor = DockerExecutor(docker_client=client)
        # owns_docker is False because we injected a client → close() must
        # NOT call client.close() on someone else's client.
        await executor.close()
        client.close.assert_not_awaited()

    async def test_close_is_idempotent(self):
        client, _container = _mock_docker()
        executor = DockerExecutor(docker_client=client)
        await executor.close()
        await executor.close()
