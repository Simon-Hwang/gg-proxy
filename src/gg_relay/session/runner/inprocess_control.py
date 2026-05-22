"""InProcessBridge — pause/resume bridge for the in-process executor.

The wire backend's :class:`WireBridge` translates pause/resume into
frames flowing across a unix socket. In-process executors share the
event loop with the runner, so we skip the wire roundtrip and let
SessionManager poke a :class:`ControlChannel` directly. The interface
mirrors :class:`WireBridge` so SessionManager can hold either flavour
behind the same ``await bridge.pause(reason=...)`` /
``await bridge.resume(hint=...)`` calls (Plan 6 D6.11 parity).
"""
from __future__ import annotations

from gg_relay.session.control import ControlAck, ControlChannel
from gg_relay.session.runner.bridge import BridgeAckTimeout


class InProcessBridge:
    """Pause/resume bridge that talks straight to a :class:`ControlChannel`.

    No frames cross a transport — the host loops the directive directly
    into the runner's :class:`ControlLoop`. The ``ack_timeout_s`` is the
    same default as :class:`WireBridge` (5s) so SessionManager behaviour
    is identical across executors when the runner stalls.
    """

    __slots__ = ("_channel",)

    def __init__(self, channel: ControlChannel) -> None:
        self._channel = channel

    @property
    def channel(self) -> ControlChannel:
        return self._channel

    async def pause(self, *, reason: str | None = None) -> ControlAck:
        """Push a pause directive and await the runner's ack.

        Raises :class:`BridgeAckTimeout` on ack timeout for parity with
        :class:`WireBridge` so the API route layer can map the timeout to
        HTTP 504 without sniffing concrete exception classes.
        """
        payload = {"reason": reason} if reason is not None else {}
        ack = await self._channel.host_request("pause", payload)
        if ack.error == "bridge_ack_timeout":
            raise BridgeAckTimeout(
                f"pause ack timeout after {self._channel.ack_timeout_s:.1f}s "
                f"req_id={ack.req_id}"
            )
        return ack

    async def resume(self, *, hint: str | None = None) -> ControlAck:
        """Push a resume directive and await the runner's ack."""
        payload = {"hint": hint} if hint is not None else {}
        ack = await self._channel.host_request("resume", payload)
        if ack.error == "bridge_ack_timeout":
            raise BridgeAckTimeout(
                f"resume ack timeout after {self._channel.ack_timeout_s:.1f}s "
                f"req_id={ack.req_id}"
            )
        return ack
