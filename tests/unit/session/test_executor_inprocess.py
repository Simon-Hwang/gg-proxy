"""Tests for InProcessExecutor (with stub runner)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.spec import PluginManifest, SessionSpec
from gg_relay.session.transport.inmemory import InMemoryTransport


async def _stub_runner(transport: InMemoryTransport, spec: SessionSpec) -> None:
    """A stub runner that just echoes one msg.chunk and ends."""
    await transport.send({  # type: ignore[arg-type]
        "v": 1, "type": "msg.chunk", "seq": 0, "ts": "2026-01-01T00:00:00Z",
        "data": {"prompt": spec.prompt},
    })
    await transport.send({  # type: ignore[arg-type]
        "v": 1, "type": "session.end", "seq": 1, "ts": "2026-01-01T00:00:00Z",
        "status": "completed",
    })


async def _drain_until_closed(transport: InMemoryTransport):
    """Async generator that yields frames until TransportClosed (spec §6.4 drain semantics)."""
    from gg_relay.session.transport import TransportClosed
    try:
        while True:
            yield await transport.recv()
    except TransportClosed:
        return


class TestInProcessExecutor:
    async def test_start_returns_handle(self, tmp_path: Path):
        exec_ = InProcessExecutor(runner=_stub_runner)
        spec = SessionSpec(
            prompt="hello",
            cwd=tmp_path,
            plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )
        handle = await exec_.start(spec)
        assert handle.backend == "inprocess"
        assert handle.transport.is_alive
        frames = []
        for _ in range(2):
            frames.append(await handle.transport.recv())
        assert frames[0]["type"] == "msg.chunk"
        assert frames[1]["type"] == "session.end"
        await exec_.stop(handle)

    async def test_stop_closes_transport(self, tmp_path: Path):
        exec_ = InProcessExecutor(runner=_stub_runner)
        spec = SessionSpec(
            prompt="hi", cwd=tmp_path, plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )
        handle = await exec_.start(spec)
        await exec_.stop(handle)
        assert handle.transport.is_alive is False
        assert await exec_.health(handle) is False

    async def test_runner_exception_propagates(self, tmp_path: Path):
        async def bad_runner(transport: InMemoryTransport, spec: SessionSpec) -> None:
            raise RuntimeError("boom")

        exec_ = InProcessExecutor(runner=bad_runner)
        spec = SessionSpec(
            prompt="hi", cwd=tmp_path, plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )
        handle = await exec_.start(spec)
        from gg_relay.session.transport import TransportClosed
        with pytest.raises(TransportClosed):
            await handle.transport.recv()
        await exec_.stop(handle)

    async def test_stop_after_runner_finished_natural(self, tmp_path: Path):
        """stop() on a runner that already returned naturally should be a no-op for cancel."""
        exec_ = InProcessExecutor(runner=_stub_runner)
        spec = SessionSpec(
            prompt="hi", cwd=tmp_path, plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )
        handle = await exec_.start(spec)
        async for _ in _drain_until_closed(handle.transport):
            pass
        # _tasks should be auto-cleaned by add_done_callback after a scheduling round
        await asyncio.sleep(0)
        assert handle.runtime_id not in exec_._tasks
        await exec_.stop(handle)
        assert handle.transport.is_alive is False

    async def test_stop_unknown_handle_is_noop(self, tmp_path: Path):
        """stop() on a handle whose runtime_id is not in _tasks is idempotent."""
        exec_ = InProcessExecutor(runner=_stub_runner)
        spec = SessionSpec(
            prompt="x", cwd=tmp_path, plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )
        handle = await exec_.start(spec)
        exec_._tasks.pop(handle.runtime_id, None)
        await exec_.stop(handle)
        assert handle.transport.is_alive is False

    async def test_concurrent_starts_independent(self, tmp_path: Path):
        """Two start() calls should yield two independent runtime_ids and transports."""
        exec_ = InProcessExecutor(runner=_stub_runner)
        spec = SessionSpec(
            prompt="hi", cwd=tmp_path, plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )
        h1, h2 = await asyncio.gather(exec_.start(spec), exec_.start(spec))
        assert h1.runtime_id != h2.runtime_id
        assert h1.transport is not h2.transport
        async for _ in _drain_until_closed(h1.transport):
            pass
        async for _ in _drain_until_closed(h2.transport):
            pass
        await exec_.stop(h1)
        await exec_.stop(h2)

    async def test_drain_after_runner_close(self, tmp_path: Path):
        """Buffered frames sent before close must be drainable on the host side (spec §6.4)."""
        async def burst_runner(transport: InMemoryTransport, spec: SessionSpec) -> None:
            for i in range(5):
                await transport.send({  # type: ignore[arg-type]
                    "v": 1, "type": "msg.chunk", "seq": i,
                    "ts": "2026-01-01T00:00:00Z",
                    "data": {"i": i},
                })
            await transport.send({  # type: ignore[arg-type]
                "v": 1, "type": "session.end", "seq": 5,
                "ts": "2026-01-01T00:00:00Z",
                "status": "completed",
            })

        exec_ = InProcessExecutor(runner=burst_runner)
        spec = SessionSpec(
            prompt="burst", cwd=tmp_path, plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )
        handle = await exec_.start(spec)
        await asyncio.sleep(0.01)
        frames = []
        from gg_relay.session.transport import TransportClosed
        try:
            while True:
                frames.append(await handle.transport.recv())
        except TransportClosed:
            pass
        assert len(frames) == 6
        assert frames[-1]["type"] == "session.end"
        await exec_.stop(handle)
