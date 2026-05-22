"""Unit tests for :class:`WireBridge` — host-side EventFrame consumer.

Uses :func:`make_pair` from the in-memory transport so we exercise the real
SessionTransport contract (recv blocks, send pushes, close sentinel).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from gg_relay.session.frames import make_pong
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.runner.bridge import WireBridge
from gg_relay.session.transport.inmemory import make_pair
from gg_relay.session.transport.protocol import (
    SessionEndFrame,
    ToolRequestFrame,
)


def _bridge(host, coord, **kw):
    """WireBridge factory that defaults heartbeat to OFF for tests that don't
    exercise it (passing ``heartbeat_interval_s=0`` disables the periodic
    sender)."""
    kw.setdefault("heartbeat_interval_s", 0)
    return WireBridge(host, coord, **kw)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _tool_request(seq: int, req_id: str, *, tool: str = "Bash") -> ToolRequestFrame:
    return cast(
        ToolRequestFrame,
        {
            "v": 1,
            "type": "tool.request",
            "seq": seq,
            "ts": _now_iso(),
            "req_id": req_id,
            "tool": tool,
            "args": {"command": "ls"},
        },
    )


def _session_end(seq: int, status: str = "completed") -> SessionEndFrame:
    return cast(
        SessionEndFrame,
        {
            "v": 1,
            "type": "session.end",
            "seq": seq,
            "ts": _now_iso(),
            "status": status,
            "tokens": {},
            "cost_usd": 0.0,
        },
    )


async def test_tool_request_routes_to_coordinator_and_replies_with_decision():
    host, runner = make_pair()
    coordinator = HITLCoordinator()
    bridge = _bridge(host, coordinator)
    run_task = asyncio.create_task(bridge.run())

    # Runner emits tool.request → bridge calls coordinator.request → waits.
    await runner.send(_tool_request(1, "r-1"))

    # Wait for the coordinator to register the pending request, then resolve.
    while not coordinator.pending_snapshot():
        await asyncio.sleep(0)
    await coordinator.resolve("r-1", "accept")

    decision_frame = await asyncio.wait_for(runner.recv(), timeout=1.0)
    assert decision_frame["type"] == "tool.decision"
    assert decision_frame["req_id"] == "r-1"
    assert decision_frame["decision"] == "accept"

    # Drive run() to completion with session.end.
    await runner.send(_session_end(2))
    await asyncio.wait_for(run_task, timeout=1.0)
    assert bridge.finished is True


async def test_pong_resets_heartbeat_miss_counter():
    """Runner replies pong → bridge updates last_pong_seq and clears the
    miss counter. We trigger this with an explicit pong frame instead of
    waiting for a real ping cycle."""
    host, runner = make_pair()
    bridge = _bridge(host, HITLCoordinator())
    # Prime a miss counter so we can prove pong actually clears it.
    bridge._heartbeat_misses = 2
    run_task = asyncio.create_task(bridge.run())

    await runner.send(cast(Any, make_pong(42)))
    # Give the run loop a tick to consume the pong.
    while bridge._heartbeat_misses != 0:
        await asyncio.sleep(0)
    assert bridge._last_pong_seq == 42

    await runner.send(_session_end(99))
    await asyncio.wait_for(run_task, timeout=1.0)


async def test_event_frames_are_buffered_for_persistence():
    host, runner = make_pair()
    bridge = _bridge(host, HITLCoordinator())
    run_task = asyncio.create_task(bridge.run())

    # install.done is the first frame; msg.chunk follows.
    await runner.send(cast(
        Any,
        {
            "v": 1, "type": "install.done", "seq": 1, "ts": _now_iso(),
            "profile_id": "minimal", "modules": ["m1"],
            "duration_ms": 12, "install_root": "/root",
        },
    ))
    await runner.send(cast(
        Any,
        {"v": 1, "type": "msg.chunk", "seq": 2, "ts": _now_iso(),
         "data": {"type": "AssistantMessage"}},
    ))
    await runner.send(_session_end(3))
    await asyncio.wait_for(run_task, timeout=1.0)

    types = [f["type"] for f in bridge.frames]
    assert types == ["install.done", "msg.chunk", "session.end"]


async def test_session_end_breaks_run_loop():
    host, runner = make_pair()
    bridge = _bridge(host, HITLCoordinator())
    run_task = asyncio.create_task(bridge.run())

    await runner.send(_session_end(1, status="completed"))
    # run() must return within a reasonable time, NOT hang on the next recv.
    await asyncio.wait_for(run_task, timeout=1.0)
    assert bridge.finished is True


async def test_transport_close_exits_run_loop():
    host, runner = make_pair()
    bridge = _bridge(host, HITLCoordinator())
    run_task = asyncio.create_task(bridge.run())

    await runner.close()
    await asyncio.wait_for(run_task, timeout=1.0)
    assert bridge.finished is True
    # No frames were captured because none were sent.
    assert bridge.frames == []


async def test_shutdown_sends_shutdown_frame_and_waits_for_session_end():
    host, runner = make_pair()
    bridge = _bridge(host, HITLCoordinator())
    run_task = asyncio.create_task(bridge.run())

    async def runner_side():
        # Wait for the host's shutdown ControlFrame, then reply with session.end.
        frame = await runner.recv()
        assert frame["type"] == "shutdown"
        await runner.send(_session_end(99, status="cancelled"))

    runner_task = asyncio.create_task(runner_side())
    # Make sure both background tasks have started running before we issue
    # shutdown() — otherwise the test races the loop-startup ordering.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await bridge.shutdown(grace=1.0)
    await asyncio.wait_for(run_task, timeout=1.0)
    await asyncio.wait_for(runner_task, timeout=1.0)

    assert bridge.frames, "expected at least the session.end frame to be buffered"
    assert bridge.frames[-1]["type"] == "session.end"
    assert bridge.frames[-1]["status"] == "cancelled"


async def test_shutdown_with_no_response_times_out_cleanly():
    """If the runner is wedged, shutdown() must NOT hang past the grace
    period."""
    host, runner = make_pair()
    bridge = _bridge(host, HITLCoordinator())
    run_task = asyncio.create_task(bridge.run())

    # Don't reply at all.
    await bridge.shutdown(grace=0.2)

    # run() should eventually exit when the transport gets closed by shutdown.
    await asyncio.wait_for(run_task, timeout=1.0)
    del runner


async def test_coordinator_failure_replies_with_deny():
    """If coordinator.request raises (e.g. backend hiccup), the bridge must
    still reply tool.decision=deny so the runner doesn't hang forever."""
    host, runner = make_pair()

    class _FailingCoordinator:
        async def request(self, req_id, *, tool, args):
            raise RuntimeError("backend down")

    bridge = _bridge(host, cast(HITLCoordinator, _FailingCoordinator()))
    run_task = asyncio.create_task(bridge.run())

    await runner.send(_tool_request(1, "r-fail"))
    decision = await asyncio.wait_for(runner.recv(), timeout=1.0)
    assert decision["type"] == "tool.decision"
    assert decision["decision"] == "deny"

    await runner.send(_session_end(2))
    await asyncio.wait_for(run_task, timeout=1.0)


async def test_shutdown_is_idempotent():
    host, runner = make_pair()
    bridge = _bridge(host, HITLCoordinator())
    run_task = asyncio.create_task(bridge.run())

    async def runner_side():
        with contextlib_suppress():
            frame = await runner.recv()
            assert frame["type"] == "shutdown"
            await runner.send(_session_end(99))

    runner_task = asyncio.create_task(runner_side())

    await bridge.shutdown(grace=0.5)
    # Second call must short-circuit, not raise / not re-send the frame.
    await bridge.shutdown(grace=0.5)
    await asyncio.wait_for(run_task, timeout=1.0)
    await asyncio.wait_for(runner_task, timeout=1.0)


def contextlib_suppress():
    import contextlib

    return contextlib.suppress(Exception)


@pytest.mark.parametrize("frame_type", ["pong", "tool.request"])
async def test_finished_flag_set_after_run_completes(frame_type: str):
    host, runner = make_pair()
    bridge = _bridge(host, HITLCoordinator())
    assert bridge.finished is False
    run_task = asyncio.create_task(bridge.run())

    if frame_type == "pong":
        await runner.send(cast(Any, make_pong(1)))
    else:
        await runner.send(_tool_request(1, "r-99"))
        while not bridge._coordinator.pending_snapshot():
            await asyncio.sleep(0)
        await bridge._coordinator.resolve("r-99", "deny")

    await runner.send(_session_end(2))
    await asyncio.wait_for(run_task, timeout=1.0)
    assert bridge.finished is True


# ── Heartbeat / Task 9 ──────────────────────────────────────────────────────


async def test_heartbeat_sends_ping_and_resets_on_pong():
    """When heartbeat is on, the bridge emits a ping; if the runner replies
    pong each cycle the miss counter never reaches the threshold."""
    host, runner = make_pair()
    bridge = WireBridge(
        host,
        HITLCoordinator(),
        heartbeat_interval_s=0.02,
        heartbeat_misses_before_unhealthy=3,
    )
    run_task = asyncio.create_task(bridge.run())

    async def faithful_runner():
        """Reply pong to every ping, then send session.end."""
        for _ in range(5):
            frame = await asyncio.wait_for(runner.recv(), timeout=1.0)
            if frame["type"] == "ping":
                await runner.send(cast(Any, make_pong(frame["seq"])))
            elif frame["type"] == "shutdown":
                break
        await runner.send(_session_end(99))

    runner_task = asyncio.create_task(faithful_runner())
    await asyncio.wait_for(runner_task, timeout=2.0)
    await asyncio.wait_for(run_task, timeout=2.0)
    assert bridge.heartbeat_unhealthy is False
    assert bridge._last_pong_seq > 0


async def test_heartbeat_marks_unhealthy_after_threshold_misses():
    """If the runner never replies pong, after threshold misses the bridge
    sets heartbeat_unhealthy + buffers an error frame + invokes the callback."""
    host, runner = make_pair()
    callback_fired = asyncio.Event()

    async def on_timeout() -> None:
        callback_fired.set()

    bridge = WireBridge(
        host,
        HITLCoordinator(),
        heartbeat_interval_s=0.02,
        heartbeat_misses_before_unhealthy=3,
        on_heartbeat_timeout=on_timeout,
    )
    run_task = asyncio.create_task(bridge.run())

    # Eat the pings the bridge will fire — but DON'T reply pong.
    async def silent_runner():
        try:
            for _ in range(10):
                await asyncio.wait_for(runner.recv(), timeout=1.0)
        except (TimeoutError, Exception):
            pass

    silent_task = asyncio.create_task(silent_runner())

    await asyncio.wait_for(callback_fired.wait(), timeout=2.0)
    assert bridge.heartbeat_unhealthy is True

    # Send session.end so run() exits cleanly.
    await runner.send(_session_end(99))
    await asyncio.wait_for(run_task, timeout=2.0)
    silent_task.cancel()
    # An error frame with code=heartbeat_timeout was buffered.
    codes = [f.get("code") for f in bridge.frames if f["type"] == "error"]
    assert "heartbeat_timeout" in codes


async def test_heartbeat_disabled_when_interval_is_zero():
    """interval=0 disables the heartbeat task; nothing should be sent over
    the wire."""
    host, runner = make_pair()
    bridge = WireBridge(host, HITLCoordinator(), heartbeat_interval_s=0)
    run_task = asyncio.create_task(bridge.run())

    await asyncio.sleep(0.05)
    # The runner shouldn't have received anything.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(runner.recv(), timeout=0.05)

    await runner.send(_session_end(1))
    await asyncio.wait_for(run_task, timeout=1.0)
    assert bridge._heartbeat_task is None


async def test_proxy_responds_to_ping_with_pong():
    """End-to-end heartbeat: WireBridge ping → WireCoordinatorProxy on the
    runner side must auto-reply pong. Bridges plan Task 3 + Task 9."""
    from gg_relay.session.runner.proxy_client import WireCoordinatorProxy

    host, runner = make_pair()
    proxy = WireCoordinatorProxy(runner)
    consume = asyncio.create_task(proxy.consume_loop())

    bridge = WireBridge(
        host,
        HITLCoordinator(),
        heartbeat_interval_s=0.02,
        heartbeat_misses_before_unhealthy=10,
    )
    run_task = asyncio.create_task(bridge.run())

    # Wait until we've seen at least 2 pongs back-to-back.
    for _ in range(200):
        if bridge._last_pong_seq >= 2:
            break
        await asyncio.sleep(0.01)
    assert bridge._last_pong_seq >= 2, (
        f"never got pong, last_seq={bridge._last_pong_seq}"
    )

    # Teardown: send session.end through the proxy's transport.
    await runner.send(_session_end(99))
    await asyncio.wait_for(run_task, timeout=2.0)
    await host.close()
    await asyncio.wait_for(consume, timeout=1.0)
