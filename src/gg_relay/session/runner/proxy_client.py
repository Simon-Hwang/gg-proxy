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

Plan 6 D6.11 adds a second responsibility: the proxy now also routes the
new ``pause`` / ``resume`` ControlFrames into a :class:`ControlChannel`
that the runner's :class:`ControlLoop` drains. Acks emitted by the
control loop are translated back into ``pause.ack`` / ``resume.ack``
EventFrames via :meth:`send_ack`.

When the transport closes mid-flight, every pending future is resolved with
``"deny"`` so the SDK call sites unblock instead of hanging forever. The
shutdown ControlFrame triggers :class:`SystemExit(0)` from the consume loop;
the runner's outer ``finally`` then tears the SDK conversation down.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Literal, cast

from gg_relay.session.control import ControlAck, ControlChannel, ControlMessage, ControlOp
from gg_relay.session.frames import make_pause_ack, make_pong, make_resume_ack
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

    Plan 6 D6.11: a :class:`ControlChannel` is exposed via
    :attr:`control_channel`; the wire runner uses it to drive a shared
    :class:`ControlLoop`. The proxy's :meth:`send_ack` callable closes
    over the transport so the runner's control loop can stay agnostic of
    the docker-vs-inprocess split.
    """

    def __init__(self, transport: SessionTransport) -> None:
        self._transport = transport
        self._pending: dict[str, asyncio.Future[Decision]] = {}
        self._shutdown_requested = False
        self._pong_seq = 0
        self._control_channel = ControlChannel()
        self._ack_seq = 0

    @property
    def shutdown_requested(self) -> bool:
        """Set to ``True`` after :meth:`consume_loop` observes a ``shutdown``
        ControlFrame. Lets the outer runner choose to exit cooperatively
        instead of relying on raise-from-task semantics."""
        return self._shutdown_requested

    @property
    def control_channel(self) -> ControlChannel:
        """Per-runner pause/resume control channel (Plan 6 D6.11).

        The wire runner spawns a :class:`ControlLoop` against this
        channel; the proxy's :meth:`consume_loop` pushes ``pause`` and
        ``resume`` ControlFrames into it.
        """
        return self._control_channel

    async def send_ack(self, ack: ControlAck) -> None:
        """Translate a :class:`ControlAck` into the matching ack EventFrame.

        Used as the :class:`ControlLoop`'s ``ack`` callback in wire
        mode. Failure to send (transport closed mid-ack) is logged via
        :class:`TransportClosed` suppression — the host's bridge will
        hit its 5s ack-timeout instead.
        """
        self._ack_seq += 1
        if ack.op == "pause":
            frame: Any = make_pause_ack(
                self._ack_seq, ack.req_id, ok=ack.ok, error=ack.error
            )
        else:
            frame = make_resume_ack(
                self._ack_seq, ack.req_id, ok=ack.ok, error=ack.error
            )
        with contextlib.suppress(TransportClosed):
            await self._transport.send(frame)

    async def request(
        self,
        req_id: str,
        *,
        tool: str,
        args: dict[str, Any],
        session_id: str = "",
    ) -> Decision:
        """Suspend until ``tool.decision`` for ``req_id`` arrives.

        Mirrors :meth:`HITLCoordinator.request` so ``client.py`` can call
        ``coordinator.request(...)`` without caring which backend it is.
        ``tool`` / ``args`` / ``session_id`` are accepted for signature
        parity; they are NOT re-sent (the caller already emitted the
        ``tool.request`` frame and the host bridge tracks session_id at
        the coordinator side).
        """
        if req_id in self._pending:
            raise ValueError(f"duplicate req_id {req_id!r}")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Decision] = loop.create_future()
        self._pending[req_id] = fut
        del tool, args, session_id
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
                elif ftype in ("pause", "resume"):
                    # Plan 6 D6.11 control-loop: route into the runner's
                    # ControlChannel. The runner's ControlLoop is the
                    # single consumer; acks travel back out via
                    # :meth:`send_ack`.
                    op = cast(ControlOp, ftype)
                    req_id = cast(str, frame.get("req_id", ""))
                    if not req_id:
                        # Defensive: drop malformed frames rather than
                        # blocking the loop on a missing correlation key.
                        continue
                    payload: dict[str, Any] = {}
                    if ftype == "pause" and isinstance(frame.get("reason"), str):
                        payload["reason"] = frame.get("reason")
                    if ftype == "resume" and isinstance(frame.get("hint"), str):
                        payload["hint"] = frame.get("hint")
                    await self._control_channel.push(
                        ControlMessage(op=op, req_id=req_id, payload=payload)
                    )
                # Unknown frames are dropped silently — the wire protocol
                # may grow new ControlFrame types without breaking older
                # runners.
        except TransportClosed:
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_result("deny")
            self._control_channel.close()
