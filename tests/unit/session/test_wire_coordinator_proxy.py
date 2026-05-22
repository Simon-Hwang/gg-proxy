"""Unit tests for :class:`WireCoordinatorProxy`.

Uses :func:`InMemoryTransport.make_pair` as the wire mock so we exercise the
real SessionTransport contract (recv blocks, send pushes, close fires EOF).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest

from gg_relay.session.runner.proxy_client import WireCoordinatorProxy
from gg_relay.session.transport.inmemory import make_pair
from gg_relay.session.transport.protocol import (
    ShutdownFrame,
    ToolDecisionFrame,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _decision_frame(seq: int, req_id: str, decision: str = "accept") -> ToolDecisionFrame:
    return cast(
        ToolDecisionFrame,
        {
            "v": 1,
            "type": "tool.decision",
            "seq": seq,
            "ts": _now_iso(),
            "req_id": req_id,
            "decision": decision,
        },
    )


def _shutdown_frame(seq: int = 99) -> ShutdownFrame:
    return cast(
        ShutdownFrame,
        {"v": 1, "type": "shutdown", "seq": seq, "ts": _now_iso()},
    )


async def test_request_blocks_until_decision_arrives():
    host, runner = make_pair()
    proxy = WireCoordinatorProxy(runner)
    consume = asyncio.create_task(proxy.consume_loop())

    request_task = asyncio.create_task(
        proxy.request("r-1", tool="Bash", args={"command": "ls"})
    )
    # Decision hasn't arrived → request must not be done yet.
    while "r-1" not in proxy._pending:
        await asyncio.sleep(0)
    assert not request_task.done()

    await host.send(_decision_frame(1, "r-1", "accept"))
    result = await asyncio.wait_for(request_task, timeout=1.0)
    assert result == "accept"

    await host.close()
    await asyncio.wait_for(consume, timeout=1.0)


async def test_duplicate_req_id_raises_value_error():
    _host, runner = make_pair()
    proxy = WireCoordinatorProxy(runner)

    first = asyncio.create_task(proxy.request("r-dup", tool="Bash", args={}))
    # Yield so the first request lands in `_pending`.
    await asyncio.sleep(0)
    with pytest.raises(ValueError, match="duplicate"):
        await proxy.request("r-dup", tool="Bash", args={})

    # Cancel the first to unblock the test cleanly.
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_consume_loop_routes_decision_to_pending_future():
    """Decisions can arrive in any order; the proxy must match by req_id."""
    host, runner = make_pair()
    proxy = WireCoordinatorProxy(runner)
    consume = asyncio.create_task(proxy.consume_loop())

    req_a = asyncio.create_task(proxy.request("r-A", tool="Bash", args={}))
    req_b = asyncio.create_task(proxy.request("r-B", tool="Bash", args={}))
    # Yield until both requests have registered themselves in `_pending`;
    # otherwise the decisions sent below could race ahead and be dropped.
    while len(proxy._pending) < 2:
        await asyncio.sleep(0)

    await host.send(_decision_frame(1, "r-B", "deny"))
    await host.send(_decision_frame(2, "r-A", "accept"))

    res_a = await asyncio.wait_for(req_a, timeout=1.0)
    res_b = await asyncio.wait_for(req_b, timeout=1.0)
    assert res_a == "accept"
    assert res_b == "deny"

    await host.close()
    await asyncio.wait_for(consume, timeout=1.0)


async def test_transport_close_resolves_pending_with_deny():
    """If the host disappears while a request is in flight, the SDK call site
    must NOT hang. Every pending future resolves to ``deny`` on cleanup."""
    host, runner = make_pair()
    proxy = WireCoordinatorProxy(runner)
    consume = asyncio.create_task(proxy.consume_loop())

    req = asyncio.create_task(proxy.request("r-orphan", tool="Bash", args={}))
    # Make sure the request is pending before we yank the rug.
    while "r-orphan" not in proxy._pending:
        await asyncio.sleep(0)
    assert not req.done()

    await host.close()
    result = await asyncio.wait_for(req, timeout=1.0)
    assert result == "deny"
    await asyncio.wait_for(consume, timeout=1.0)


async def test_shutdown_frame_sets_flag_and_exits_loop():
    host, runner = make_pair()
    proxy = WireCoordinatorProxy(runner)
    consume = asyncio.create_task(proxy.consume_loop())

    assert proxy.shutdown_requested is False
    await host.send(_shutdown_frame())
    await asyncio.wait_for(consume, timeout=1.0)
    assert proxy.shutdown_requested is True

    await host.close()


async def test_unknown_frame_is_silently_dropped():
    """Forward-compat: new ControlFrame types added in v2 must not crash an
    older runner. The proxy should ignore them and keep serving."""
    host, runner = make_pair()
    proxy = WireCoordinatorProxy(runner)
    consume = asyncio.create_task(proxy.consume_loop())

    # An unknown ``ping``-like frame.
    await host.send(cast(
        ToolDecisionFrame,  # cheating the type for the test
        {"v": 1, "type": "future_op", "seq": 1, "ts": _now_iso()},
    ))
    # Then a real decision must still route.
    req = asyncio.create_task(proxy.request("r-X", tool="Bash", args={}))
    while "r-X" not in proxy._pending:
        await asyncio.sleep(0)
    await host.send(_decision_frame(2, "r-X", "accept"))
    result = await asyncio.wait_for(req, timeout=1.0)
    assert result == "accept"

    await host.close()
    await asyncio.wait_for(consume, timeout=1.0)
