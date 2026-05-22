"""End-to-end integration: SDK-dataclass stub yields real ``claude_code_sdk``
dataclasses, drained through the executor → frames assertion.

Plan 2 Task 7 — exercises the runner against the actual dataclass dispatch
without hitting the Anthropic API. Companion to
``test_real_api_smoke.py`` (which DOES hit the API behind
``@pytest.mark.requires_api_key``).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_code_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from gg_relay.session.client import make_sdk_runner
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import DEFAULT_POLICY
from gg_relay.session.spec import PluginManifest, SessionSpec
from gg_relay.session.transport.protocol import TransportClosed

pytestmark = pytest.mark.requires_sdk


def _spec(tmp_path: Path) -> SessionSpec:
    return SessionSpec(
        prompt="x",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
    )


class _StubBaseClient:
    def __init__(self, options: Any) -> None:
        self.options = options

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        return None

    async def receive_messages(self) -> AsyncIterator[Any]:  # pragma: no cover
        if False:
            yield None


async def _drain(handle, *, timeout: float = 2.0) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=timeout)
        except (TimeoutError, TransportClosed):
            break
        frames.append(dict(f))
        if f["type"] == "session.end":
            break
    return frames


# ── 1. Basic dataclass round-trip ─────────────────────────────────────────


async def test_dataclass_dispatch_basic_round_trip(tmp_path: Path) -> None:
    """AssistantMessage(TextBlock) + ResultMessage → msg.chunk + session.end."""

    class _C(_StubBaseClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            yield AssistantMessage(
                content=[TextBlock(text="I will help.")],
                model="claude-test",
            )
            yield ResultMessage(
                subtype="success", duration_ms=42, duration_api_ms=40,
                is_error=False, num_turns=1, session_id="sess-abc",
                total_cost_usd=0.001, usage={"input_tokens": 10, "output_tokens": 5},
            )

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=lambda opts: _C(opts),
    )
    executor = InProcessExecutor(runner=runner)
    handle = await executor.start(_spec(tmp_path))
    frames = await _drain(handle)
    await executor.stop(handle)

    types = [f["type"] for f in frames]
    assert "msg.chunk" in types
    assert types[-1] == "session.end"
    end = frames[-1]
    assert end["status"] == "completed"
    assert end["tokens"] == {"input_tokens": 10, "output_tokens": 5}
    assert end["cost_usd"] == 0.001


# ── 2. tool_use → tool_result id mapping ──────────────────────────────────


async def test_dataclass_tool_use_to_result_id_mapping(tmp_path: Path) -> None:
    """req_id propagates from tool.request → tool.result via FIFO map."""
    coord = HITLCoordinator()

    class _C(_StubBaseClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            ctx = ToolPermissionContext(signal=None, suggestions=[])
            await self.options.can_use_tool("Bash", {"command": "uname -a"}, ctx)
            yield AssistantMessage(
                content=[
                    TextBlock(text="Running uname"),
                    ToolUseBlock(
                        id="toolu_smoke",
                        name="Bash",
                        input={"command": "uname -a"},
                    ),
                ],
                model="claude-test",
            )
            yield UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="toolu_smoke",
                        content="Linux gg-host",
                        is_error=False,
                    )
                ],
            )
            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id="s",
            )

    async def approver() -> None:
        for _ in range(50):
            snap = coord.pending_snapshot()
            if snap:
                await coord.resolve(next(iter(snap)), "accept")
                return
            await asyncio.sleep(0.02)

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=coord,
        sdk_factory=lambda opts: _C(opts),
    )
    executor = InProcessExecutor(runner=runner)
    asyncio.create_task(approver())
    handle = await executor.start(_spec(tmp_path))
    frames = await _drain(handle)
    await executor.stop(handle)

    req = next(f for f in frames if f["type"] == "tool.request")
    res = next(f for f in frames if f["type"] == "tool.result")
    assert req["req_id"] == res["req_id"]
    assert req["req_id"].startswith("r-")
    assert res["ok"] is True


# ── 3. ResultMessage tokens + cost propagate ──────────────────────────────


async def test_dataclass_result_message_tokens_and_cost_propagate(
    tmp_path: Path,
) -> None:
    class _C(_StubBaseClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            yield ResultMessage(
                subtype="success",
                duration_ms=1200,
                duration_api_ms=1100,
                is_error=False,
                num_turns=3,
                session_id="sess-xyz",
                total_cost_usd=0.0234,
                usage={
                    "input_tokens": 1000,
                    "output_tokens": 250,
                    "cache_creation_input_tokens": 42,
                },
            )

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=lambda opts: _C(opts),
    )
    executor = InProcessExecutor(runner=runner)
    handle = await executor.start(_spec(tmp_path))
    frames = await _drain(handle)
    await executor.stop(handle)

    end = next(f for f in frames if f["type"] == "session.end")
    assert end["cost_usd"] == pytest.approx(0.0234)
    assert end["tokens"]["input_tokens"] == 1000
    assert end["tokens"]["output_tokens"] == 250
    assert end["tokens"]["cache_creation_input_tokens"] == 42


# ── 4. Same tool same input twice — FIFO order preserved ──────────────────


async def test_dataclass_concurrent_same_tool_fifo_ordering(
    tmp_path: Path,
) -> None:
    """Two same-name same-input tool calls in the same turn; FIFO order."""
    coord = HITLCoordinator()

    class _C(_StubBaseClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            ctx = ToolPermissionContext(signal=None, suggestions=[])
            # Two perms back-to-back; both pending until pairing
            await self.options.can_use_tool("Bash", {"command": "echo hi"}, ctx)
            await self.options.can_use_tool("Bash", {"command": "echo hi"}, ctx)
            yield AssistantMessage(
                content=[
                    ToolUseBlock(id="tu_first", name="Bash", input={"command": "echo hi"}),
                    ToolUseBlock(id="tu_second", name="Bash", input={"command": "echo hi"}),
                ],
                model="claude-test",
            )
            yield UserMessage(
                content=[
                    ToolResultBlock(tool_use_id="tu_first", content="hi", is_error=False),
                    ToolResultBlock(tool_use_id="tu_second", content="hi", is_error=False),
                ],
            )
            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id="s",
            )

    async def approver() -> None:
        seen = 0
        while seen < 2:
            snap = coord.pending_snapshot()
            for rid in list(snap):
                await coord.resolve(rid, "accept")
                seen += 1
            await asyncio.sleep(0.02)

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=coord,
        sdk_factory=lambda opts: _C(opts),
    )
    executor = InProcessExecutor(runner=runner)
    asyncio.create_task(approver())
    handle = await executor.start(_spec(tmp_path))
    frames = await _drain(handle, timeout=3.0)
    await executor.stop(handle)

    req_ids = [f["req_id"] for f in frames if f["type"] == "tool.request"]
    res_ids = [f["req_id"] for f in frames if f["type"] == "tool.result"]
    assert len(req_ids) == 2
    assert len(res_ids) == 2
    # First request pairs with first ToolUseBlock → first result
    assert req_ids == res_ids
    assert req_ids[0] != req_ids[1]


# ── 5. Unmapped tool_use_id → empty req_id (defensive) ────────────────────


async def test_dataclass_unmapped_tool_result_empty_req_id(tmp_path: Path) -> None:
    """ToolResultBlock whose tool_use_id was never registered → empty req_id."""

    class _C(_StubBaseClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            yield UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="phantom_id",
                        content="ghost",
                        is_error=False,
                    )
                ],
            )
            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=0, session_id="s",
            )

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=lambda opts: _C(opts),
    )
    executor = InProcessExecutor(runner=runner)
    handle = await executor.start(_spec(tmp_path))
    frames = await _drain(handle)
    await executor.stop(handle)

    res = next(f for f in frames if f["type"] == "tool.result")
    assert res["req_id"] == ""
    # is_error=False → ok=True
    assert res["ok"] is True
