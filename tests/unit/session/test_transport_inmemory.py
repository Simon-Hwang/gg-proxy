"""Tests for InMemoryTransport."""
from __future__ import annotations

import asyncio

import pytest

from gg_relay.session.transport import TransportClosed
from gg_relay.session.transport.inmemory import InMemoryTransport, make_pair


def _ping() -> dict:
    return {"v": 1, "type": "ping", "seq": 0, "ts": "2026-01-01T00:00:00Z"}


def _pong(seq: int = 0) -> dict:
    return {"v": 1, "type": "pong", "seq": seq, "ts": "2026-01-01T00:00:00Z"}


class TestInMemoryTransportPair:
    async def test_send_recv_roundtrip(self):
        host, runner = make_pair()
        assert isinstance(host, InMemoryTransport)
        assert isinstance(runner, InMemoryTransport)
        await host.send(_ping())  # type: ignore[arg-type]
        frame = await runner.recv()
        assert frame["type"] == "ping"

        await runner.send(_pong(seq=1))  # type: ignore[arg-type]
        frame = await host.recv()
        assert frame["type"] == "pong"
        assert frame["seq"] == 1

    async def test_close_propagates(self):
        host, runner = make_pair()
        await host.close()
        assert host.is_alive is False
        assert runner.is_alive is False
        with pytest.raises(TransportClosed):
            await host.send(_ping())  # type: ignore[arg-type]
        with pytest.raises(TransportClosed):
            await runner.recv()

    async def test_recv_blocks_until_send(self):
        host, runner = make_pair()

        async def delayed_send():
            await asyncio.sleep(0.01)
            await host.send(_ping())  # type: ignore[arg-type]

        task = asyncio.create_task(delayed_send())
        frame = await asyncio.wait_for(runner.recv(), timeout=0.5)
        await task
        assert frame["type"] == "ping"

    async def test_send_recv_ordering(self):
        host, runner = make_pair()
        for i in range(5):
            await host.send(_pong(seq=i))  # type: ignore[arg-type]
        seqs = []
        for _ in range(5):
            f = await runner.recv()
            seqs.append(f["seq"])
        assert seqs == [0, 1, 2, 3, 4]

    async def test_close_propagates_reverse(self):
        """Closing the runner side should propagate to the host side (reverse direction)."""
        host, runner = make_pair()
        await runner.close()
        assert host.is_alive is False
        assert runner.is_alive is False
        with pytest.raises(TransportClosed):
            await runner.send(_pong())  # type: ignore[arg-type]
        with pytest.raises(TransportClosed):
            await host.recv()

    async def test_recv_blocks_explicitly(self):
        """Verify recv() truly blocks instead of just being slower than wait_for timeout."""
        host, runner = make_pair()
        recv_task = asyncio.create_task(runner.recv())
        await asyncio.sleep(0)
        assert recv_task.done() is False, "recv() should still be waiting on empty queue"
        await host.send(_ping())  # type: ignore[arg-type]
        frame = await asyncio.wait_for(recv_task, timeout=0.5)
        assert frame["type"] == "ping"

    async def test_close_is_idempotent(self):
        """Double-close on the same side hits the early-return path (line 58)."""
        host, runner = make_pair()
        await host.close()
        await host.close()
        await runner.close()
        assert host.is_alive is False
        assert runner.is_alive is False

    async def test_recv_sentinel_path_coverage(self):
        """Sentinel is the sole close-detection path in recv() (POSIX EOF semantics).

        recv() does not eagerly fail on `is_alive == False`; it drains all
        buffered EventFrames first and only raises `TransportClosed` when it
        sees `_CLOSE_SENTINEL` in the inbound queue (spec §6.4).
        This test exercises the close-detection path directly by injecting
        the sentinel into an otherwise-alive transport.
        """
        from gg_relay.session.transport.inmemory import _CLOSE_SENTINEL, InMemoryTransport
        inbound: asyncio.Queue[object] = asyncio.Queue()
        outbound: asyncio.Queue[object] = asyncio.Queue()
        t = InMemoryTransport(inbound=inbound, outbound=outbound)
        assert t.is_alive is True
        await inbound.put(_CLOSE_SENTINEL)
        with pytest.raises(TransportClosed, match="peer closed"):
            await t.recv()
        assert t.is_alive is False
