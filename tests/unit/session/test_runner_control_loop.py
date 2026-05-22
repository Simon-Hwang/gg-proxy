"""ControlLoop / wire pause-resume round-trip tests (Plan 6 Task 2 / D6.11).

Covers:
* ControlChannel host_request → runner_recv → runner_ack happy path.
* host_request timeout when no ack arrives within ``ack_timeout_s``.
* ControlLoop pause/resume against a stub SDK client (no real
  ``ClaudeSDKClient`` import).
* Double-pause / orphan-resume rejection (``already_paused`` /
  ``not_paused``) with the runner-side state machine.
* SDK exceptions surface as ``ok=False`` acks instead of crashing the
  loop (so the host doesn't hang on ``await bridge.pause()``).
* WireBridge.pause/resume round-trip through the in-memory transport pair.
* Bridge timeout raises :class:`BridgeAckTimeout` when the runner side
  never replies.
* JSON-round-trip parity for the four new frame builders.
* InProcessBridge integrates with the same ControlChannel.
* control_loop honours strict FIFO ordering across pause/resume directives.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

import pytest

from gg_relay.session.control import (
    ControlAck,
    ControlChannel,
    ControlLoop,
    ControlMessage,
)
from gg_relay.session.frames import (
    make_pause,
    make_pause_ack,
    make_resume,
    make_resume_ack,
)
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.runner.bridge import BridgeAckTimeout, WireBridge
from gg_relay.session.runner.inprocess_control import InProcessBridge
from gg_relay.session.runner.proxy_client import WireCoordinatorProxy
from gg_relay.session.transport.inmemory import make_pair


class _StubClient:
    """Minimal duck-type of ClaudeSDKClient surface ControlLoop needs."""

    def __init__(
        self,
        *,
        interrupt_raises: Exception | None = None,
        query_raises: Exception | None = None,
    ) -> None:
        self.interrupt_calls = 0
        self.queries: list[str] = []
        self._interrupt_raises = interrupt_raises
        self._query_raises = query_raises

    async def interrupt(self) -> None:
        self.interrupt_calls += 1
        if self._interrupt_raises is not None:
            raise self._interrupt_raises

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        if self._query_raises is not None:
            raise self._query_raises


# ── Frame builders ────────────────────────────────────────────────────────


class TestFrameBuilders:
    def test_pause_envelope_shape(self):
        f = make_pause(7, "pause-1", reason="user")
        assert f == {
            "v": 1,
            "type": "pause",
            "seq": 7,
            "ts": f["ts"],
            "req_id": "pause-1",
            "reason": "user",
        }

    def test_pause_omits_reason_when_none(self):
        f = make_pause(7, "pause-1")
        assert "reason" not in f

    def test_resume_with_hint(self):
        f = make_resume(8, "resume-1", hint="keep going")
        assert f["type"] == "resume"
        assert f["hint"] == "keep going"
        assert f["req_id"] == "resume-1"

    def test_pause_ack_envelope_shape(self):
        f = make_pause_ack(9, "pause-1", ok=True)
        assert f["type"] == "pause.ack"
        assert f["ok"] is True
        assert f["req_id"] == "pause-1"
        assert "error" not in f

    def test_resume_ack_with_error(self):
        f = make_resume_ack(10, "resume-1", ok=False, error="not_paused")
        assert f["type"] == "resume.ack"
        assert f["ok"] is False
        assert f["error"] == "not_paused"


# ── ControlChannel basics ─────────────────────────────────────────────────


class TestControlChannel:
    async def test_host_request_round_trip(self):
        ch = ControlChannel(ack_timeout_s=1.0)

        async def runner() -> None:
            msg = await ch.runner_recv()
            assert msg.op == "pause"
            assert msg.payload == {"reason": "user"}
            await ch.runner_ack(ControlAck(op="pause", req_id=msg.req_id, ok=True))

        task = asyncio.create_task(runner())
        ack = await ch.host_request("pause", {"reason": "user"})
        assert ack.ok and ack.error is None
        await task

    async def test_host_request_times_out(self):
        ch = ControlChannel(ack_timeout_s=0.05)
        ack = await ch.host_request("pause", {"reason": "lonely"})
        assert ack.ok is False
        assert ack.error == "bridge_ack_timeout"

    async def test_close_resolves_pending_with_error(self):
        ch = ControlChannel(ack_timeout_s=5.0)
        # Register a pending future without ever pulling/acking
        host_task = asyncio.create_task(ch.host_request("pause", {}))
        await asyncio.sleep(0)  # let host enter wait_for
        # Drain the inbox so a future call wouldn't fire
        _ = await ch.runner_recv()
        ch.close()
        ack = await host_task
        assert ack.ok is False
        assert ack.error == "channel_closed"


# ── ControlLoop semantics ─────────────────────────────────────────────────


async def _drive_loop_until_done(loop: ControlLoop, ack_q: list[ControlAck]) -> None:
    """Helper: spin until the loop finishes (caller cancels it)."""
    try:
        await loop.run()
    finally:
        del ack_q  # acks were captured via the AckSender closure


class TestControlLoop:
    async def test_pause_then_resume_happy_path(self):
        client = _StubClient()
        inbox: asyncio.Queue[ControlMessage] = asyncio.Queue()
        acks: list[ControlAck] = []

        async def ack(a: ControlAck) -> None:
            acks.append(a)

        loop = ControlLoop(client=client, recv=inbox.get, ack=ack)
        task = asyncio.create_task(loop.run())
        await inbox.put(ControlMessage(op="pause", req_id="p1"))
        await inbox.put(ControlMessage(op="resume", req_id="r1", payload={"hint": "hi"}))
        # Wait until both acks land
        for _ in range(50):
            if len(acks) >= 2:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.interrupt_calls == 1
        assert client.queries == ["hi"]
        assert acks[0] == ControlAck(op="pause", req_id="p1", ok=True)
        assert acks[1] == ControlAck(op="resume", req_id="r1", ok=True)

    async def test_double_pause_returns_already_paused(self):
        client = _StubClient()
        inbox: asyncio.Queue[ControlMessage] = asyncio.Queue()
        acks: list[ControlAck] = []

        async def ack(a: ControlAck) -> None:
            acks.append(a)

        loop = ControlLoop(client=client, recv=inbox.get, ack=ack)
        task = asyncio.create_task(loop.run())
        await inbox.put(ControlMessage(op="pause", req_id="p1"))
        await inbox.put(ControlMessage(op="pause", req_id="p2"))
        for _ in range(50):
            if len(acks) >= 2:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert acks[0].ok is True
        assert acks[1].ok is False
        assert acks[1].error == "already_paused"
        # interrupt() called exactly once even though two pause msgs flowed.
        assert client.interrupt_calls == 1

    async def test_resume_without_pause_returns_not_paused(self):
        client = _StubClient()
        inbox: asyncio.Queue[ControlMessage] = asyncio.Queue()
        acks: list[ControlAck] = []

        async def ack(a: ControlAck) -> None:
            acks.append(a)

        loop = ControlLoop(client=client, recv=inbox.get, ack=ack)
        task = asyncio.create_task(loop.run())
        await inbox.put(ControlMessage(op="resume", req_id="r0"))
        for _ in range(50):
            if acks:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert acks == [ControlAck(op="resume", req_id="r0", ok=False, error="not_paused")]
        assert client.queries == []

    async def test_sdk_exception_surfaces_as_ok_false(self):
        client = _StubClient(interrupt_raises=RuntimeError("sdk timeout"))
        inbox: asyncio.Queue[ControlMessage] = asyncio.Queue()
        acks: list[ControlAck] = []

        async def ack(a: ControlAck) -> None:
            acks.append(a)

        loop = ControlLoop(client=client, recv=inbox.get, ack=ack)
        task = asyncio.create_task(loop.run())
        await inbox.put(ControlMessage(op="pause", req_id="p1"))
        for _ in range(50):
            if acks:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert acks[0].ok is False
        assert acks[0].error is not None
        assert "RuntimeError" in acks[0].error

    async def test_fifo_ordering(self):
        client = _StubClient()
        inbox: asyncio.Queue[ControlMessage] = asyncio.Queue()
        acks: list[ControlAck] = []

        async def ack(a: ControlAck) -> None:
            acks.append(a)

        loop = ControlLoop(client=client, recv=inbox.get, ack=ack)
        task = asyncio.create_task(loop.run())
        # Bulk-push four messages; loop must process them in order.
        for op, rid in (("pause", "p1"), ("resume", "r1"), ("pause", "p2"), ("resume", "r2")):
            await inbox.put(ControlMessage(op=op, req_id=rid))  # type: ignore[arg-type]
        for _ in range(100):
            if len(acks) >= 4:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [a.req_id for a in acks] == ["p1", "r1", "p2", "r2"]
        assert all(a.ok for a in acks)


# ── WireBridge pause/resume round-trip ────────────────────────────────────


class TestWireBridgePauseRoundTrip:
    async def test_pause_and_resume_through_transport(self):
        host_t, runner_t = make_pair()
        coordinator = HITLCoordinator()
        bridge = WireBridge(host_t, coordinator, heartbeat_interval_s=0.0)
        proxy = WireCoordinatorProxy(runner_t)
        client = _StubClient()
        proxy_consume = asyncio.create_task(proxy.consume_loop())
        loop = ControlLoop(
            client=client,
            recv=proxy.control_channel.runner_recv,
            ack=proxy.send_ack,
        )
        loop_task = asyncio.create_task(loop.run())
        bridge_run = asyncio.create_task(bridge.run())

        try:
            ack = await bridge.pause(reason="user")
            assert ack.ok is True
            assert ack.op == "pause"
            assert client.interrupt_calls == 1
            ack2 = await bridge.resume(hint="go on")
            assert ack2.ok is True
            assert client.queries == ["go on"]
        finally:
            await bridge.shutdown(grace=0.5)
            loop_task.cancel()
            proxy_consume.cancel()
            for t in (loop_task, proxy_consume, bridge_run):
                with contextlib.suppress(
                    asyncio.CancelledError, SystemExit, Exception
                ):
                    await t

    async def test_pause_times_out_without_runner(self):
        host_t, _runner_t = make_pair()
        coordinator = HITLCoordinator()
        bridge = WireBridge(
            host_t, coordinator, heartbeat_interval_s=0.0, ack_timeout_s=0.05
        )
        bridge_run = asyncio.create_task(bridge.run())
        try:
            with pytest.raises(BridgeAckTimeout):
                await bridge.pause(reason="lonely")
        finally:
            await bridge.shutdown(grace=0.1)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await bridge_run


# ── InProcessBridge integration ───────────────────────────────────────────


class TestInProcessBridge:
    async def test_pause_then_resume_via_inprocess_channel(self):
        ch = ControlChannel(ack_timeout_s=1.0)
        bridge = InProcessBridge(ch)
        client = _StubClient()

        loop = ControlLoop(client=client, recv=ch.runner_recv, ack=ch.runner_ack)
        loop_task = asyncio.create_task(loop.run())
        try:
            ack = await bridge.pause(reason="user")
            assert ack.ok is True
            assert client.interrupt_calls == 1
            ack2 = await bridge.resume(hint="continue please")
            assert ack2.ok is True
            assert client.queries == ["continue please"]
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task

    async def test_inprocess_bridge_timeout_raises(self):
        ch = ControlChannel(ack_timeout_s=0.05)
        bridge = InProcessBridge(ch)
        with pytest.raises(BridgeAckTimeout):
            await bridge.pause(reason="no runner")


# ── ProxyClient frame routing ─────────────────────────────────────────────


class TestProxyClientControlRouting:
    async def test_pause_frame_lands_on_control_channel(self):
        host_t, runner_t = make_pair()
        proxy = WireCoordinatorProxy(runner_t)
        consume = asyncio.create_task(proxy.consume_loop())
        try:
            # Inject a pause frame from the host side; the proxy must
            # route it into the control channel inbox.
            await host_t.send(
                cast(Any, make_pause(1, "p1", reason="user_pause"))
            )
            msg = await asyncio.wait_for(proxy.control_channel.runner_recv(), timeout=0.5)
            assert msg.op == "pause"
            assert msg.req_id == "p1"
            assert msg.payload == {"reason": "user_pause"}
        finally:
            consume.cancel()
            with contextlib.suppress(asyncio.CancelledError, SystemExit, Exception):
                await consume

    async def test_send_ack_writes_pause_ack_frame(self):
        host_t, runner_t = make_pair()
        proxy = WireCoordinatorProxy(runner_t)
        await proxy.send_ack(ControlAck(op="pause", req_id="p9", ok=True))
        frame = await asyncio.wait_for(host_t.recv(), timeout=0.5)
        assert frame["type"] == "pause.ack"
        assert frame["req_id"] == "p9"
        assert frame["ok"] is True
