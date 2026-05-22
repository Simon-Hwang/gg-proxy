"""Walking-skeleton end-to-end demo.

Run:
  source .venv/bin/activate
  python examples/walking_skeleton_demo.py

This uses a stub SDK (no real API call). It demonstrates:
  1. handler builds SessionSpec
  2. InProcessExecutor starts runner
  3. host consumes EventFrames from transport
  4. NEEDS_HITL frame is auto-approved by a side-task (simulating an IM responder)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_code_sdk import (
    AssistantMessage,
    ResultMessage,
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


class _DemoSDK:
    """Fake SDK that requests Write (auto-accept) then Bash (HITL) then ends.

    Duck-typed against ClaudeSDKClient; consumed via make_sdk_runner's
    `sdk_factory` hook so the demo runs without a real API key.
    `options` is typed as Any because the runner only reads `.can_use_tool`
    and `.cwd`, and constraining to ClaudeCodeOptions here adds no safety.
    """

    def __init__(self, options: Any) -> None:
        self._options = options

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        return None

    async def receive_messages(self) -> AsyncIterator[Any]:
        ctx = ToolPermissionContext(signal=None, suggestions=[])

        # Write under cwd → policy ACCEPT → no HITL round-trip
        write_file = str(Path(self._options.cwd) / "demo.txt")
        write_input = {"file_path": write_file, "content": "hello"}
        write_result = await self._options.can_use_tool("Write", write_input, ctx)
        if write_result.behavior == "allow":
            yield AssistantMessage(
                content=[ToolUseBlock(id="tu_write", name="Write", input=write_input)],
                model="demo-stub",
            )
            yield UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="tu_write",
                        content=f"wrote {write_file}",
                        is_error=False,
                    )
                ],
            )

        # Bash → policy NEEDS_HITL → runner publishes tool.request, awaits coordinator
        bash_input = {"command": "ls /tmp"}
        bash_result = await self._options.can_use_tool("Bash", bash_input, ctx)
        if bash_result.behavior == "allow":
            yield AssistantMessage(
                content=[ToolUseBlock(id="tu_bash", name="Bash", input=bash_input)],
                model="demo-stub",
            )
            yield UserMessage(
                content=[
                    ToolResultBlock(tool_use_id="tu_bash", content="ok", is_error=False)
                ],
            )

        yield ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=2,
            session_id="demo-session",
            total_cost_usd=0.0,
            usage={"input_tokens": 0, "output_tokens": 0},
        )


async def im_responder(coord: HITLCoordinator) -> None:
    """Simulate an IM user approving HITL requests as soon as they appear."""
    for _ in range(100):
        snap = coord.pending_snapshot()
        if snap:
            for req_id, info in snap.items():
                print(f"  [IM] approving {req_id}: tool={info['tool']} args={info['args']}")
                await coord.resolve(req_id, "accept")
            return
        await asyncio.sleep(0.05)


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        coord = HITLCoordinator()
        runner = make_sdk_runner(
            policy=DEFAULT_POLICY,
            coordinator=coord,
            sdk_factory=_DemoSDK,
        )
        executor = InProcessExecutor(runner=runner)

        spec = SessionSpec(
            prompt="demo prompt",
            cwd=cwd,
            plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )

        print(f"▶ Starting session in {cwd}")
        # Keep a reference so the responder isn't garbage-collected mid-flight.
        responder_task = asyncio.create_task(im_responder(coord))
        try:
            handle = await executor.start(spec)
            print(f"▶ runtime_id={handle.runtime_id}\n")

            while True:
                try:
                    frame = await asyncio.wait_for(handle.transport.recv(), timeout=3.0)
                except TimeoutError:
                    print("◀ timeout waiting for frame; aborting")
                    break
                except TransportClosed:
                    print("◀ transport closed by runner")
                    break
                print(f"◀ frame: {json.dumps(frame, default=str)[:200]}")
                if frame["type"] == "session.end":
                    break

            await executor.stop(handle)
        finally:
            responder_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await responder_task

        print("\n✓ session ended cleanly")


if __name__ == "__main__":
    asyncio.run(main())
