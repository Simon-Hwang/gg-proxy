"""WireCoordinatorProxy — container-side HITLCoordinator stand-in.

In the in-process backend, ``client.py:make_sdk_runner`` calls
``coordinator.request(req_id, tool=..., args=...)`` directly because the
runner and the host share an event loop. In the docker backend, the runner is
inside the container and the host owns the real HITLCoordinator. This proxy
gives the runner a ``request()`` with the same signature; under the hood it:

  1. Trusts that the caller (``make_wire_runner``) already emitted a
     ``tool.request`` EventFrame onto the wire transport.
  2. Suspends on an asyncio.Future keyed by ``req_id``.
  3. ``consume_loop()`` (must be running as a sibling task) reads incoming
     ControlFrames and resolves the matching future when ``tool.decision``
     arrives.

When the transport closes mid-flight, every pending future is resolved with
``"deny"`` so the SDK call sites unblock instead of hanging forever. The
shutdown ControlFrame triggers :class:`SystemExit(0)` from the consume loop;
the runner's outer ``finally`` then tears the SDK conversation down.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Literal, cast

from gg_relay.session.frames import make_pong
from gg_relay.session.transport.protocol import (
    SessionTransport,
    TransportClosed,
)

Decision = Literal["accept", "deny"]


class WireCoordinatorProxy:
    """Container-side duck-type of :class:`HITLCoordinator`.

    The proxy reads ControlFrames; the runner writes EventFrames. There is
    therefore no concurrent reader on the transport's ``recv()`` channel; the
    consume loop is the single owner. The runner side never calls
    ``transport.recv()`` directly.
    """

    def __init__(self, transport: SessionTransport) -> None:
        self._transport = transport
        self._pending: dict[str, asyncio.Future[Decision]] = {}
        self._shutdown_requested = False
        self._pong_seq = 0

    @property
    def shutdown_requested(self) -> bool:
        """Set to ``True`` after :meth:`consume_loop` observes a ``shutdown``
        ControlFrame. Lets the outer runner choose to exit cooperatively
        instead of relying on raise-from-task semantics."""
        return self._shutdown_requested

    async def request(
        self,
        req_id: str,
        *,
        tool: str,
        args: dict[str, Any],
    ) -> Decision:
        """Suspend until ``tool.decision`` for ``req_id`` arrives.

        Mirrors :meth:`HITLCoordinator.request` so ``client.py`` can call
        ``coordinator.request(...)`` without caring which backend it is.
        ``tool`` / ``args`` are accepted for signature parity; they are NOT
        re-sent (the caller already emitted the ``tool.request`` frame).
        """
        if req_id in self._pending:
            raise ValueError(f"duplicate req_id {req_id!r}")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Decision] = loop.create_future()
        self._pending[req_id] = fut
        # Refer to the args so static checkers (and unused-arg linters) are
        # happy without us pretending to do something with them on the wire.
        del tool, args
        try:
            return await fut
        finally:
            self._pending.pop(req_id, None)

    async def consume_loop(self) -> None:
        """Route incoming ControlFrames to pending requests.

        Exits cleanly when the transport closes OR when a ``shutdown`` frame
        arrives (in which case :attr:`shutdown_requested` is set and the loop
        returns; the caller decides how to tear the SDK conversation down).
        Any pending futures are resolved with ``"deny"`` on exit so SDK
        callers do not hang.
        """
        try:
            while True:
                frame = await self._transport.recv()
                ftype = frame.get("type")
                if ftype == "tool.decision":
                    req_id = cast(str, frame.get("req_id", ""))
                    decision = cast(Decision, frame.get("decision", "deny"))
                    fut = self._pending.get(req_id)
                    if fut is not None and not fut.done():
                        fut.set_result(decision)
                elif ftype == "shutdown":
                    self._shutdown_requested = True
                    return
                elif ftype == "ping":
                    # Container-side heartbeat reply (Plan 3 D3.10). The host
                    # bridge sends ping every interval; if we miss 3 in a row
                    # the host marks us unhealthy and calls executor.stop().
                    self._pong_seq += 1
                    with contextlib.suppress(TransportClosed):
                        await self._transport.send(make_pong(self._pong_seq))
                # Unknown frames are dropped silently — the wire protocol
                # may grow new ControlFrame types without breaking older
                # runners.
        except TransportClosed:
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_result("deny")
