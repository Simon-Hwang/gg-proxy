"""End-to-end walking skeleton: SessionSpec → InProcessExecutor → SDK runner → events.

Uses a stub SDK transport (claude_code_sdk.Transport subclass) so no API call is made.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.requires_sdk


async def test_walking_skeleton_completes(tmp_path: Path) -> None:
    """handler → InProcessExecutor → stub-SDK runner → see msg.chunk + session.end."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.hitl.policy import DEFAULT_POLICY
    from gg_relay.session.spec import PluginManifest, SessionSpec

    coord = HITLCoordinator()
    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=coord,
        sdk_factory=_make_stub_sdk_client,
    )
    executor = InProcessExecutor(runner=runner)

    spec = SessionSpec(
        prompt="say hello",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
    )
    handle = await executor.start(spec)

    frames: list[dict[str, Any]] = []
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=1.0)
        except Exception:
            break
        frames.append(dict(f))
        if f["type"] == "session.end":
            break

    await executor.stop(handle)

    types = [f["type"] for f in frames]
    assert "msg.chunk" in types
    assert "session.end" in types
    assert frames[-1]["status"] == "completed"


async def test_walking_skeleton_auto_accept_write(tmp_path: Path) -> None:
    """When stub SDK requests a Write inside cwd, can_use_tool returns Allow."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.hitl.policy import DEFAULT_POLICY
    from gg_relay.session.spec import PluginManifest, SessionSpec

    target = tmp_path / "out.txt"
    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=lambda options: _StubWriteAttemptClient(options, file_path=str(target)),
    )
    executor = InProcessExecutor(runner=runner)
    spec = SessionSpec(
        prompt="write a file",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
    )
    handle = await executor.start(spec)

    decisions: list[bool] = []
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=1.0)
        except Exception:
            break
        if f["type"] == "tool.result":
            decisions.append(bool(f["ok"]))  # type: ignore[typeddict-item]
        if f["type"] == "session.end":
            break
    await executor.stop(handle)
    assert decisions == [True]   # the Write was allowed


async def test_walking_skeleton_hitl_path_blocks_then_approves(tmp_path: Path) -> None:
    """Bash request → policy says NEEDS_HITL → coord resolved externally → allowed."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.hitl.policy import DEFAULT_POLICY
    from gg_relay.session.spec import PluginManifest, SessionSpec

    coord = HITLCoordinator()

    async def auto_approve_after_delay() -> None:
        # wait until something is pending, then approve
        for _ in range(50):
            snap = coord.pending_snapshot()
            if snap:
                req_id = next(iter(snap))
                await coord.resolve(req_id, "accept")
                return
            await asyncio.sleep(0.02)

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=coord,
        sdk_factory=lambda options: _StubBashAttemptClient(options),
    )
    executor = InProcessExecutor(runner=runner)
    spec = SessionSpec(
        prompt="run ls",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
    )
    asyncio.create_task(auto_approve_after_delay())

    handle = await executor.start(spec)
    bash_result_ok = False
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=2.0)
        except Exception:
            break
        if f["type"] == "tool.result" and f.get("ok") is True:  # type: ignore[typeddict-item]
            bash_result_ok = True
        if f["type"] == "session.end":
            break
    await executor.stop(handle)
    assert bash_result_ok, "Bash tool should have been approved via HITL"


async def test_walking_skeleton_hitl_path_deny(tmp_path: Path) -> None:
    """coord.resolve(req_id, 'deny') → can_use_tool returns Deny → stub sees behavior == 'deny'."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.hitl.policy import DEFAULT_POLICY
    from gg_relay.session.spec import PluginManifest, SessionSpec

    coord = HITLCoordinator()

    async def auto_deny_after_delay() -> None:
        for _ in range(50):
            snap = coord.pending_snapshot()
            if snap:
                req_id = next(iter(snap))
                await coord.resolve(req_id, "deny", reason="not safe")
                return
            await asyncio.sleep(0.02)

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=coord,
        sdk_factory=lambda options: _StubBashAttemptClient(options),
    )
    executor = InProcessExecutor(runner=runner)
    spec = SessionSpec(
        prompt="run ls", cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"), executor="inprocess",
    )
    asyncio.create_task(auto_deny_after_delay())

    handle = await executor.start(spec)
    bash_result_ok: bool | None = None
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=2.0)
        except Exception:
            break
        if f["type"] == "tool.result":
            bash_result_ok = bool(f["ok"])  # type: ignore[typeddict-item]
        if f["type"] == "session.end":
            break
    await executor.stop(handle)
    assert bash_result_ok is False, "Bash tool should have been denied via HITL"


async def test_walking_skeleton_policy_deny(tmp_path: Path) -> None:
    """A custom ToolPolicy that returns Decision.DENY → can_use_tool returns Deny."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.spec import Decision, PluginManifest, SessionSpec

    class _AlwaysDenyPolicy:
        """Mock policy that returns DENY for Bash, ACCEPT for everything else."""

        def decide(self, tool: str, args: object, cwd: object) -> Decision:
            if tool == "Bash":
                return Decision.DENY
            return Decision.ACCEPT

    runner = make_sdk_runner(
        policy=_AlwaysDenyPolicy(),  # type: ignore[arg-type]
        coordinator=HITLCoordinator(),
        sdk_factory=lambda options: _StubBashAttemptClient(options),
    )
    executor = InProcessExecutor(runner=runner)
    spec = SessionSpec(
        prompt="ls", cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"), executor="inprocess",
    )
    handle = await executor.start(spec)
    bash_result_ok: bool | None = None
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=2.0)
        except Exception:
            break
        if f["type"] == "tool.result":
            bash_result_ok = bool(f["ok"])  # type: ignore[typeddict-item]
        if f["type"] == "session.end":
            break
    await executor.stop(handle)
    assert bash_result_ok is False, "Policy DENY should have produced PermissionResultDeny"


async def test_walking_skeleton_error_frame_on_runner_exception(tmp_path: Path) -> None:
    """Runner exception → error frame published before TransportClosed."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.hitl.policy import DEFAULT_POLICY
    from gg_relay.session.spec import PluginManifest, SessionSpec

    class _ExplodingClient(_StubBaseClient):
        async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
            if False:  # make this an async-generator without yielding
                yield {}
            raise RuntimeError("simulated SDK failure")

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=lambda options: _ExplodingClient(options),
    )
    executor = InProcessExecutor(runner=runner)
    spec = SessionSpec(
        prompt="x", cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"), executor="inprocess",
    )
    handle = await executor.start(spec)
    seen_error = False
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=1.0)
        except Exception:
            break
        if f["type"] == "error":
            seen_error = True
            assert f["code"] == "RuntimeError"  # type: ignore[typeddict-item]
            assert "simulated SDK failure" in f["message"]  # type: ignore[typeddict-item]
            break
    await executor.stop(handle)
    assert seen_error, "error frame must be emitted before close on runner exception"


async def test_walking_skeleton_factory_exception_publishes_error(tmp_path: Path) -> None:
    """sdk_factory itself raising → error frame published (I-1 regression)."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.hitl.policy import DEFAULT_POLICY
    from gg_relay.session.spec import PluginManifest, SessionSpec

    def _exploding_factory(_options: Any) -> Any:
        raise RuntimeError("factory boom")

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=_exploding_factory,
    )
    executor = InProcessExecutor(runner=runner)
    spec = SessionSpec(
        prompt="x", cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"), executor="inprocess",
    )
    handle = await executor.start(spec)
    seen_error = False
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=1.0)
        except Exception:
            break
        if f["type"] == "error":
            assert f["code"] == "RuntimeError"  # type: ignore[typeddict-item]
            assert "factory boom" in f["message"]  # type: ignore[typeddict-item]
            seen_error = True
            break
    await executor.stop(handle)
    assert seen_error, "factory exception must publish an error frame (I-1)"


async def test_walking_skeleton_cancellation_no_false_error(tmp_path: Path) -> None:
    """executor.stop() mid-flight must NOT publish error frame with code=CancelledError (I-2)."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.hitl.policy import DEFAULT_POLICY
    from gg_relay.session.spec import PluginManifest, SessionSpec

    class _NeverEndsClient(_StubBaseClient):
        async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
            while True:
                await asyncio.sleep(0.05)
                yield {"type": "AssistantMessage", "content": "still going"}

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=lambda options: _NeverEndsClient(options),
    )
    executor = InProcessExecutor(runner=runner)
    spec = SessionSpec(
        prompt="x", cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"), executor="inprocess",
    )
    handle = await executor.start(spec)

    # Drain at least one frame to ensure runner is actively yielding
    await asyncio.wait_for(handle.transport.recv(), timeout=1.0)
    # Now yank the runner
    await executor.stop(handle)

    # Drain remaining frames; assert no CancelledError-coded error frame
    seen_false_error = False
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=0.3)
        except Exception:
            break
        if f["type"] == "error" and f.get("code") == "CancelledError":  # type: ignore[typeddict-item]
            seen_false_error = True
            break
    assert not seen_false_error, "cancellation must not surface as an error frame (I-2)"


# ── Stub SDK clients (avoid hitting real API) ──────────────────────────────


class _StubBaseClient:
    """Common stub matching the subset of ClaudeSDKClient we use."""

    def __init__(self, options: Any) -> None:
        self._options = options

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def query(self, prompt: str) -> None: ...
    async def interrupt(self) -> None: ...


def _make_stub_sdk_client(options: Any) -> _StubBaseClient:
    """Minimal stub: just yields one assistant message and ends."""

    class _C(_StubBaseClient):
        async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "AssistantMessage", "content": "hi"}
            yield {"type": "ResultMessage", "subtype": "success", "total_cost_usd": 0.0}

    return _C(options)


class _StubWriteAttemptClient(_StubBaseClient):
    """Stub that triggers options.can_use_tool with a Write request."""

    def __init__(self, options: Any, file_path: str) -> None:
        super().__init__(options)
        self._file_path = file_path

    async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
        from claude_code_sdk import ToolPermissionContext
        ctx = ToolPermissionContext(signal=None, suggestions=[])
        result = await self._options.can_use_tool(
            "Write", {"file_path": self._file_path, "content": "x"}, ctx
        )
        ok = result.behavior == "allow"
        yield {
            "type": "ToolResult",
            "tool_name": "Write",
            "ok": ok,
            "result": {"file_path": self._file_path},
        }
        yield {"type": "ResultMessage", "subtype": "success", "total_cost_usd": 0.0}


class _StubBashAttemptClient(_StubBaseClient):
    async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
        from claude_code_sdk import ToolPermissionContext
        ctx = ToolPermissionContext(signal=None, suggestions=[])
        result = await self._options.can_use_tool(
            "Bash", {"command": "ls"}, ctx
        )
        ok = result.behavior == "allow"
        yield {
            "type": "ToolResult",
            "tool_name": "Bash",
            "ok": ok,
            "result": {"stdout": "."},
        }
        yield {"type": "ResultMessage", "subtype": "success", "total_cost_usd": 0.0}
