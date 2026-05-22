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

    async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
        from claude_code_sdk import ToolPermissionContext

        ctx = ToolPermissionContext(signal=None, suggestions=[])

        # Write under cwd → policy ACCEPT → no HITL round-trip
        write_result = await self._options.can_use_tool(
            "Write",
            {
                "file_path": str(Path(self._options.cwd) / "demo.txt"),
                "content": "hello",
            },
            ctx,
        )
        yield {
            "type": "ToolResult",
            "tool_name": "Write",
            "ok": write_result.behavior == "allow",
        }

        # Bash → policy NEEDS_HITL → runner publishes tool.request, awaits coordinator
        bash_result = await self._options.can_use_tool(
            "Bash",
            {"command": "ls /tmp"},
            ctx,
        )
        yield {
            "type": "ToolResult",
            "tool_name": "Bash",
            "ok": bash_result.behavior == "allow",
        }

        yield {"type": "ResultMessage", "subtype": "success", "total_cost_usd": 0.0}


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
