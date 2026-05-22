"""Tests for client.py SDK-dataclass dispatch + bidirectional FIFO mapping
(Plan 2 Task 3)."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_code_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_code_sdk.types import StreamEvent

from gg_relay.session.client import make_sdk_runner
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import DEFAULT_POLICY
from gg_relay.session.spec import PluginManifest, SessionSpec
from gg_relay.session.transport.protocol import TransportClosed


def _make_spec(tmp_path: Path) -> SessionSpec:
    return SessionSpec(
        prompt="x",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
    )


class _StubClient:
    """Minimal SDK stub. Subclasses override receive_messages()."""

    def __init__(self, options: Any) -> None:
        self.options = options

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def receive_messages(self) -> AsyncIterator[Any]:  # pragma: no cover
        if False:
            yield None


async def _drain(handle, *, timeout: float = 1.0) -> list[dict[str, Any]]:
    """Drain frames until session.end or transport close."""
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


def _run_session(
    tmp_path: Path,
    sdk_factory,
    *,
    policy=DEFAULT_POLICY,
    coordinator: HITLCoordinator | None = None,
):
    """Helper: build executor + runner, return (executor, handle, coord)."""
    coord = coordinator or HITLCoordinator()
    runner = make_sdk_runner(
        policy=policy,
        coordinator=coord,
        sdk_factory=sdk_factory,
    )
    executor = InProcessExecutor(runner=runner)
    spec = _make_spec(tmp_path)
    return executor, spec, coord


# ── 1. ResultMessage emits session.end with tokens + cost ─────────────────


async def test_result_message_emits_session_end_with_tokens_and_cost(
    tmp_path: Path,
) -> None:
    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            yield ResultMessage(
                subtype="success",
                duration_ms=1200,
                duration_api_ms=1100,
                is_error=False,
                num_turns=1,
                session_id="s1",
                total_cost_usd=0.0042,
                usage={"input_tokens": 100, "output_tokens": 50},
            )

    executor, spec, _ = _run_session(tmp_path, lambda opts: _C(opts))
    handle = await executor.start(spec)
    frames = await _drain(handle)
    await executor.stop(handle)

    end = next(f for f in frames if f["type"] == "session.end")
    assert end["status"] == "completed"
    assert end["tokens"] == {"input_tokens": 100, "output_tokens": 50}
    assert end["cost_usd"] == 0.0042


async def test_result_message_handles_none_cost_and_usage(tmp_path: Path) -> None:
    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            yield ResultMessage(
                subtype="success",
                duration_ms=0,
                duration_api_ms=0,
                is_error=False,
                num_turns=0,
                session_id="s",
                total_cost_usd=None,
                usage=None,
            )

    executor, spec, _ = _run_session(tmp_path, lambda opts: _C(opts))
    handle = await executor.start(spec)
    frames = await _drain(handle)
    await executor.stop(handle)

    end = next(f for f in frames if f["type"] == "session.end")
    assert end["cost_usd"] == 0.0
    assert end["tokens"] == {}


# ── 2. AssistantMessage emits msg.chunk ────────────────────────────────────


async def test_assistant_message_emits_msg_chunk(tmp_path: Path) -> None:
    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            yield AssistantMessage(
                content=[TextBlock(text="hello")],
                model="claude-sonnet-test",
            )
            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id="s",
            )

    executor, spec, _ = _run_session(tmp_path, lambda opts: _C(opts))
    handle = await executor.start(spec)
    frames = await _drain(handle)
    await executor.stop(handle)

    chunks = [f for f in frames if f["type"] == "msg.chunk"]
    assert len(chunks) == 1
    data = chunks[0]["data"]
    # Body must include the text block; serialization shape is implementation
    # detail but must be a dict and round-trippable.
    assert isinstance(data, dict)
    assert "hello" in repr(data)


# ── 3. UserMessage(ToolResultBlock) → tool.result with mapped req_id ───────


async def test_user_message_with_tool_result_emits_tool_result_with_mapped_req_id(
    tmp_path: Path,
) -> None:
    """End-to-end: HITL approves Bash; ToolUseBlock pairs with req_id via FIFO;
    later ToolResultBlock surfaces with the mapped req_id."""

    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            # 1. host's can_use_tool is invoked first by triggering perm check
            ctx = ToolPermissionContext(signal=None, suggestions=[])
            result = await self.options.can_use_tool("Bash", {"command": "ls"}, ctx)
            assert result.behavior == "allow"
            # 2. assistant message with the ToolUseBlock
            yield AssistantMessage(
                content=[ToolUseBlock(id="toolu_xyz", name="Bash", input={"command": "ls"})],
                model="m",
            )
            # 3. user message with the result
            yield UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="toolu_xyz", content="ok", is_error=False
                    )
                ],
            )
            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id="s",
            )

    coord = HITLCoordinator()

    async def auto_approve() -> None:
        for _ in range(50):
            snap = coord.pending_snapshot()
            if snap:
                await coord.resolve(next(iter(snap)), "accept")
                return
            await asyncio.sleep(0.02)

    executor, spec, _ = _run_session(tmp_path, lambda opts: _C(opts), coordinator=coord)
    asyncio.create_task(auto_approve())
    handle = await executor.start(spec)
    frames = await _drain(handle, timeout=2.0)
    await executor.stop(handle)

    req_frames = [f for f in frames if f["type"] == "tool.request"]
    res_frames = [f for f in frames if f["type"] == "tool.result"]
    assert len(req_frames) == 1
    assert len(res_frames) == 1
    # req_id must propagate from tool.request → tool.result via FIFO mapping.
    assert req_frames[0]["req_id"] == res_frames[0]["req_id"]
    assert res_frames[0]["ok"] is True


# ── 4. FIFO single call ────────────────────────────────────────────────────


async def test_fifo_mapping_single_call(tmp_path: Path) -> None:
    """1 HITL can_use_tool → 1 ToolUseBlock → 1 ToolResultBlock; req_id flows through.

    Bash triggers NEEDS_HITL (policy returns NEEDS_HITL by default), so a req_id
    is generated and the FIFO mapping is exercised end-to-end.
    """

    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            ctx = ToolPermissionContext(signal=None, suggestions=[])
            await self.options.can_use_tool("Bash", {"command": "ls"}, ctx)
            yield AssistantMessage(
                content=[ToolUseBlock(id="tu_1", name="Bash", input={"command": "ls"})],
                model="m",
            )
            yield UserMessage(
                content=[ToolResultBlock(tool_use_id="tu_1", content="ok")],
            )
            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id="s",
            )

    coord = HITLCoordinator()

    async def auto_approve() -> None:
        for _ in range(50):
            snap = coord.pending_snapshot()
            if snap:
                await coord.resolve(next(iter(snap)), "accept")
                return
            await asyncio.sleep(0.02)

    executor, spec, _ = _run_session(tmp_path, lambda opts: _C(opts), coordinator=coord)
    asyncio.create_task(auto_approve())
    handle = await executor.start(spec)
    frames = await _drain(handle, timeout=2.0)
    await executor.stop(handle)

    req = next(f for f in frames if f["type"] == "tool.request")
    res = next(f for f in frames if f["type"] == "tool.result")
    assert req["req_id"] == res["req_id"]
    assert res["req_id"] != ""


# ── 5. FIFO two sequential calls ───────────────────────────────────────────


async def test_fifo_mapping_two_sequential_calls(tmp_path: Path) -> None:
    """Two different sequential Bash calls; each gets the right req_id."""
    coord = HITLCoordinator()

    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            ctx = ToolPermissionContext(signal=None, suggestions=[])
            await self.options.can_use_tool("Bash", {"command": "ls"}, ctx)
            yield AssistantMessage(
                content=[ToolUseBlock(id="tu_a", name="Bash", input={"command": "ls"})],
                model="m",
            )
            yield UserMessage(content=[ToolResultBlock(tool_use_id="tu_a", content="a")])

            await self.options.can_use_tool("Bash", {"command": "pwd"}, ctx)
            yield AssistantMessage(
                content=[ToolUseBlock(id="tu_b", name="Bash", input={"command": "pwd"})],
                model="m",
            )
            yield UserMessage(content=[ToolResultBlock(tool_use_id="tu_b", content="b")])

            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id="s",
            )

    async def auto_approve_all() -> None:
        seen = 0
        while seen < 2:
            snap = coord.pending_snapshot()
            for rid in list(snap):
                await coord.resolve(rid, "accept")
                seen += 1
            await asyncio.sleep(0.02)

    executor, spec, _ = _run_session(tmp_path, lambda opts: _C(opts), coordinator=coord)
    asyncio.create_task(auto_approve_all())
    handle = await executor.start(spec)
    frames = await _drain(handle, timeout=3.0)
    await executor.stop(handle)

    req_ids_in_order = [f["req_id"] for f in frames if f["type"] == "tool.request"]
    res_ids_in_order = [f["req_id"] for f in frames if f["type"] == "tool.result"]
    assert len(req_ids_in_order) == 2
    assert len(res_ids_in_order) == 2
    assert req_ids_in_order == res_ids_in_order


# ── 6. FIFO same tool same input twice ─────────────────────────────────────


async def test_fifo_mapping_same_name_same_input_twice(tmp_path: Path) -> None:
    """Same Bash command twice: FIFO must preserve order — first req pairs
    with first ToolUseBlock, second with second."""
    coord = HITLCoordinator()

    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            ctx = ToolPermissionContext(signal=None, suggestions=[])
            # Two perms first (matching spike scenario 3); both pending
            await self.options.can_use_tool("Bash", {"command": "ls"}, ctx)
            yield AssistantMessage(
                content=[ToolUseBlock(id="tu_1st", name="Bash", input={"command": "ls"})],
                model="m",
            )
            yield UserMessage(content=[ToolResultBlock(tool_use_id="tu_1st", content="1")])

            await self.options.can_use_tool("Bash", {"command": "ls"}, ctx)
            yield AssistantMessage(
                content=[ToolUseBlock(id="tu_2nd", name="Bash", input={"command": "ls"})],
                model="m",
            )
            yield UserMessage(content=[ToolResultBlock(tool_use_id="tu_2nd", content="2")])

            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=2, session_id="s",
            )

    async def auto_approve_all() -> None:
        seen = 0
        while seen < 2:
            snap = coord.pending_snapshot()
            for rid in list(snap):
                await coord.resolve(rid, "accept")
                seen += 1
            await asyncio.sleep(0.02)

    executor, spec, _ = _run_session(tmp_path, lambda opts: _C(opts), coordinator=coord)
    asyncio.create_task(auto_approve_all())
    handle = await executor.start(spec)
    frames = await _drain(handle, timeout=3.0)
    await executor.stop(handle)

    req_ids = [f["req_id"] for f in frames if f["type"] == "tool.request"]
    res_ids = [f["req_id"] for f in frames if f["type"] == "tool.result"]
    assert len(req_ids) == 2
    assert len(res_ids) == 2
    # FIFO pairing: results arrive in same order as requests
    assert req_ids == res_ids
    assert req_ids[0] != req_ids[1]


# ── 7. Unknown tool_use_id → empty req_id (defensive) ──────────────────────


async def test_fifo_mapping_unknown_tool_use_id_yields_empty_req_id(
    tmp_path: Path,
) -> None:
    """ToolResultBlock with unmapped tool_use_id must not crash; emit empty."""

    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            # No can_use_tool, no AssistantMessage; just a phantom result.
            yield UserMessage(
                content=[ToolResultBlock(tool_use_id="never_seen", content="?")],
            )
            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=0, session_id="s",
            )

    executor, spec, _ = _run_session(tmp_path, lambda opts: _C(opts))
    handle = await executor.start(spec)
    frames = await _drain(handle)
    await executor.stop(handle)

    res = next(f for f in frames if f["type"] == "tool.result")
    assert res["req_id"] == ""
    assert res["ok"] is True  # is_error is None / falsy → ok=True


# ── 8. SystemMessage / StreamEvent → msg.chunk ─────────────────────────────


async def test_system_and_stream_messages_emit_msg_chunk(tmp_path: Path) -> None:
    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            yield SystemMessage(subtype="init", data={"foo": "bar"})
            yield StreamEvent(uuid="u1", session_id="s", event={"kind": "delta"})
            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=0, session_id="s",
            )

    executor, spec, _ = _run_session(tmp_path, lambda opts: _C(opts))
    handle = await executor.start(spec)
    frames = await _drain(handle)
    await executor.stop(handle)

    chunks = [f for f in frames if f["type"] == "msg.chunk"]
    assert len(chunks) == 2


# ── 9. Runner exception emits error frame (regression from Plan 1) ─────────


async def test_runner_exception_emits_error_frame(tmp_path: Path) -> None:
    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            if False:
                yield None
            raise RuntimeError("kaboom")

    executor, spec, _ = _run_session(tmp_path, lambda opts: _C(opts))
    handle = await executor.start(spec)
    frames = await _drain(handle)
    await executor.stop(handle)

    err = next(f for f in frames if f["type"] == "error")
    assert err["code"] == "RuntimeError"
    assert "kaboom" in err["message"]


# ── 10. Cancellation emits no false error frame (regression from Plan 1) ───


async def test_cancellation_does_not_emit_error_frame(tmp_path: Path) -> None:
    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            while True:
                await asyncio.sleep(0.05)
                yield AssistantMessage(
                    content=[TextBlock(text="still going")], model="m",
                )

    executor, spec, _ = _run_session(tmp_path, lambda opts: _C(opts))
    handle = await executor.start(spec)
    # Drain at least one frame so the runner is actively pumping
    await asyncio.wait_for(handle.transport.recv(), timeout=1.0)
    await executor.stop(handle)

    # Drain rest; assert no CancelledError-coded error frame
    saw_cancel_err = False
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=0.3)
        except (TimeoutError, TransportClosed):
            break
        if f["type"] == "error" and f.get("code") == "CancelledError":
            saw_cancel_err = True
            break
    assert not saw_cancel_err


# ── 11. (extra) Hybrid ordering — AssistantMessage before can_use_tool ─────


async def test_fifo_mapping_assistant_arrives_before_can_use_tool(
    tmp_path: Path,
) -> None:
    """Defensive: if the AssistantMessage(ToolUseBlock) is yielded BEFORE
    can_use_tool fires (reverse of the typical CLI order), the bidirectional
    FIFO must still pair them correctly."""

    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            ctx = ToolPermissionContext(signal=None, suggestions=[])
            # 1. AssistantMessage with the tool_use FIRST (queues in pending_use_blocks)
            yield AssistantMessage(
                content=[ToolUseBlock(id="tu_rev", name="Bash", input={"command": "ls"})],
                model="m",
            )
            # Let dispatch land
            await asyncio.sleep(0.01)
            # 2. can_use_tool fires — pending_perms is empty, pending_use_blocks has tu_rev
            #    bidirectional FIFO pairs them
            await self.options.can_use_tool("Bash", {"command": "ls"}, ctx)
            # 3. result arrives
            yield UserMessage(
                content=[ToolResultBlock(tool_use_id="tu_rev", content="ok")],
            )
            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id="s",
            )

    coord = HITLCoordinator()

    async def auto_approve() -> None:
        for _ in range(50):
            snap = coord.pending_snapshot()
            if snap:
                await coord.resolve(next(iter(snap)), "accept")
                return
            await asyncio.sleep(0.02)

    executor, spec, _ = _run_session(tmp_path, lambda opts: _C(opts), coordinator=coord)
    asyncio.create_task(auto_approve())
    handle = await executor.start(spec)
    frames = await _drain(handle, timeout=2.0)
    await executor.stop(handle)

    req_frames = [f for f in frames if f["type"] == "tool.request"]
    res_frames = [f for f in frames if f["type"] == "tool.result"]
    assert len(req_frames) == 1
    assert len(res_frames) == 1
    # Bidirectional FIFO must propagate req_id even when AssistantMessage arrived first.
    assert req_frames[0]["req_id"] == res_frames[0]["req_id"]
    assert req_frames[0]["req_id"] != ""


# ── 12. (extra) Frozen-input handles nested dict ───────────────────────────


@pytest.mark.parametrize(
    "nested_input",
    [
        {"a": 1, "b": [1, 2, 3]},
        {"a": {"x": 1, "y": 2}, "b": "s"},
        {"keys": ["k1", "k2"], "config": {"n": 7}},
    ],
)
async def test_fifo_freeze_handles_nested_input(
    tmp_path: Path, nested_input: dict[str, Any],
) -> None:
    """_freeze() must canonicalize nested mutables so equality holds."""

    class _C(_StubClient):
        async def receive_messages(self) -> AsyncIterator[Any]:
            ctx = ToolPermissionContext(signal=None, suggestions=[])
            await self.options.can_use_tool("Bash", nested_input, ctx)
            yield AssistantMessage(
                content=[ToolUseBlock(id="tu_n", name="Bash", input=nested_input)],
                model="m",
            )
            yield UserMessage(
                content=[ToolResultBlock(tool_use_id="tu_n", content="ok")],
            )
            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id="s",
            )

    coord = HITLCoordinator()

    async def auto_approve() -> None:
        for _ in range(50):
            snap = coord.pending_snapshot()
            if snap:
                await coord.resolve(next(iter(snap)), "accept")
                return
            await asyncio.sleep(0.02)

    executor, spec, _ = _run_session(tmp_path, lambda opts: _C(opts), coordinator=coord)
    asyncio.create_task(auto_approve())
    handle = await executor.start(spec)
    frames = await _drain(handle, timeout=2.0)
    await executor.stop(handle)

    req = next(f for f in frames if f["type"] == "tool.request")
    res = next(f for f in frames if f["type"] == "tool.result")
    assert req["req_id"] == res["req_id"]
