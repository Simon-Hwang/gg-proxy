"""InMemoryTransport — for InProcessExecutor.

Two coupled queues: outbound from one side is inbound to the other.
make_pair() returns (host_side, runner_side).
"""
from __future__ import annotations

import asyncio
from typing import cast

from gg_relay.session.transport.protocol import (
    ControlFrame,
    EventFrame,
    TransportClosed,
)

_CLOSE_SENTINEL: object = object()


class InMemoryTransport:
    """Implements SessionTransport with two asyncio.Queue.

    send() writes to outbound; recv() reads from inbound.
    Closing propagates to the paired transport via the sentinel.
    """

    def __init__(
        self,
        inbound: asyncio.Queue[object],
        outbound: asyncio.Queue[object],
        paired: InMemoryTransport | None = None,
    ) -> None:
        self._inbound = inbound
        self._outbound = outbound
        self._paired = paired
        self._alive = True

    @property
    def is_alive(self) -> bool:
        return self._alive

    async def send(self, frame: ControlFrame | EventFrame) -> None:
        if not self._alive:
            raise TransportClosed("transport closed")
        await self._outbound.put(frame)

    async def recv(self) -> EventFrame:
        # Intentionally no early `_alive` check: buffered frames that were
        # written by the peer before close() must still be deliverable
        # (standard pipe/socket EOF semantics). The sentinel marks end-of-stream.
        item = await self._inbound.get()
        if item is _CLOSE_SENTINEL:
            self._alive = False
            raise TransportClosed("peer closed")
        return cast(EventFrame, item)

    async def close(self) -> None:
        if not self._alive:
            return
        self._alive = False
        await self._outbound.put(_CLOSE_SENTINEL)
        if self._paired is not None and self._paired._alive:
            await self._paired.close()


def make_pair(
    maxsize: int = 1024,
) -> tuple[InMemoryTransport, InMemoryTransport]:
    """Return (host_side, runner_side) — frames sent by host arrive at runner.recv."""
    q_h2r: asyncio.Queue[object] = asyncio.Queue(maxsize=maxsize)
    q_r2h: asyncio.Queue[object] = asyncio.Queue(maxsize=maxsize)
    host = InMemoryTransport(inbound=q_r2h, outbound=q_h2r)
    runner = InMemoryTransport(inbound=q_h2r, outbound=q_r2h, paired=host)
    host._paired = runner
    return host, runner
