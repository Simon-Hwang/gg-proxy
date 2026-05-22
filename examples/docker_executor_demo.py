"""Docker backend end-to-end demo (Plan 3 Task 11).

Wires together the host-side pieces introduced by Plan 3 so you can see
the full event flow:

    Host                                Container
    ─────                               ─────────
    DockerExecutor.start(spec, ctx)
       └─ UnixSocketServer (host socket)
       └─ docker run runner image  ──▶  wire_runner.py
                                          └─ UnixSocketTransport.connect
                                          └─ WireCoordinatorProxy
                                          └─ SDK loop (stub or real CLI)
    WireBridge.run(transport, coord)
       ├─ tool.request    ─────▶  HITLCoordinator.request
       ├─ tool.decision   ◀─────  IM responder approval
       └─ ping            ─────▶  pong (heartbeat)

Two modes, switched via the ``DOCKER_AVAILABLE`` env var:

* ``DOCKER_AVAILABLE=true`` (default) — runs the real ``DockerExecutor``
  against ``$GG_RELAY_RUNNER_IMAGE`` (default
  ``gg-relay-runner:dev``). Requires Docker, ``ANTHROPIC_API_KEY``, and
  a built runner image; this is the manual smoke-test path.

* ``DOCKER_AVAILABLE=false`` — replaces ``DockerExecutor`` with an
  in-process stub that does NOT shell out to docker but instead spawns
  a Python coroutine driving an in-process stub SDK over a real
  Unix-socket transport pair. This is what we run in CI / dev
  containers where neither Docker nor an API key is available, and it
  still proves the WireBridge ↔ WireCoordinatorProxy ↔
  UnixSocketTransport triple end-to-end.

Run:

    source .venv/bin/activate
    DOCKER_AVAILABLE=false python examples/docker_executor_demo.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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

from gg_relay.session.client import make_wire_runner
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import DEFAULT_POLICY
from gg_relay.session.runner.bridge import WireBridge
from gg_relay.session.runner.proxy_client import WireCoordinatorProxy
from gg_relay.session.spec import (
    PluginManifest,
    RuntimeHandle,
    SessionRuntimeContext,
    SessionSpec,
)
from gg_relay.session.transport.protocol import TransportClosed
from gg_relay.session.transport.unixsocket import (
    UnixSocketServer,
    UnixSocketTransport,
)

# ── stub SDK (identical philosophy to walking_skeleton_demo._DemoSDK) ───────


class _DemoSDK:
    """Fake SDK that does Write (auto-accept) then Bash (HITL) then ends."""

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

        write_file = str(Path(self._options.cwd) / "demo.txt")
        write_input = {"file_path": write_file, "content": "hello-from-docker-demo"}
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

        bash_input = {"command": "ls /tmp"}
        bash_result = await self._options.can_use_tool("Bash", bash_input, ctx)
        if bash_result.behavior == "allow":
            yield AssistantMessage(
                content=[ToolUseBlock(id="tu_bash", name="Bash", input=bash_input)],
                model="demo-stub",
            )
            yield UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="tu_bash", content="ok", is_error=False
                    )
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


# ── stub "container" ───────────────────────────────────────────────────────


async def _stub_runner_side(
    socket_path: Path,
    spec: SessionSpec,
) -> None:
    """In-process replacement for the runner container.

    Connects to the host's Unix socket, builds a WireCoordinatorProxy,
    and runs the same runner core that wire_runner.py drives in
    production. The only thing missing vs the real container is the
    docker daemon and the claude CLI subprocess.
    """
    transport = await UnixSocketTransport.connect(socket_path)
    proxy = WireCoordinatorProxy(transport=transport)
    consume_task = asyncio.create_task(proxy.consume_loop())
    runner = make_wire_runner(
        policy=DEFAULT_POLICY,
        coordinator=proxy,
        sdk_factory=_DemoSDK,
    )
    try:
        await runner(transport, spec)
    finally:
        consume_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consume_task
        with contextlib.suppress(Exception):
            await transport.close()


# ── IM responder ───────────────────────────────────────────────────────────


async def _im_responder(coord: HITLCoordinator) -> None:
    for _ in range(200):
        snap = coord.pending_snapshot()
        if snap:
            for req_id, info in snap.items():
                print(
                    f"  [IM] approving {req_id}: tool={info['tool']} "
                    f"args={info['args']}"
                )
                await coord.resolve(req_id, "accept")
            return
        await asyncio.sleep(0.05)


# ── frame consumer ─────────────────────────────────────────────────────────


async def _drain_until_session_end(bridge: WireBridge, *, deadline_s: float) -> bool:
    """Poll ``bridge.frames`` until we see ``session.end`` or timeout.

    Prints each frame as it appears. Returns ``True`` if session.end was
    observed within ``deadline_s`` seconds.
    """
    printed = 0
    end_t = asyncio.get_event_loop().time() + deadline_s
    while asyncio.get_event_loop().time() < end_t:
        frames = bridge.frames
        while printed < len(frames):
            frame = frames[printed]
            printed += 1
            print(f"◀ frame: {json.dumps(frame, default=str)[:180]}")
            if frame["type"] == "session.end":
                return True
        await asyncio.sleep(0.05)
    return False


# ── stub-mode entry point ──────────────────────────────────────────────────


async def _run_stub_path() -> None:
    """End-to-end with the local-runner stub (no Docker, no API key)."""
    # Avoid hitting Linux's 108-char AF_UNIX path limit on long tmp dirs.
    socket_root = Path(tempfile.mkdtemp(prefix="ggrd-", dir="/tmp"))

    coord = HITLCoordinator()
    spec = SessionSpec(
        prompt="demo prompt",
        cwd=socket_root,
        plugins=PluginManifest(profile="minimal"),
        executor="docker",
    )

    print(f"▶ [stub mode] socket_root={socket_root}")
    runtime_id = uuid.uuid4().hex
    socket_path = socket_root / f"{runtime_id}.sock"
    server = await UnixSocketServer.listen(socket_path)
    runner_task = asyncio.create_task(_stub_runner_side(socket_path, spec))
    transport = await server.accept(timeout=10.0)
    handle = RuntimeHandle(
        backend="docker-stub",
        runtime_id=runtime_id,
        transport=transport,
        started_at=datetime.now(UTC),
    )
    print(f"▶ runtime_id={handle.runtime_id}")

    bridge = WireBridge(
        transport=handle.transport,
        coordinator=coord,
        heartbeat_interval_s=0.0,  # disable for short demo
    )
    bridge_task = asyncio.create_task(bridge.run())
    responder_task = asyncio.create_task(_im_responder(coord))

    try:
        ended = await _drain_until_session_end(bridge, deadline_s=10.0)
        if not ended:
            print("◀ no session.end after 10s; aborting")
        await bridge.shutdown()
    finally:
        bridge_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, TransportClosed):
            await bridge_task
        with contextlib.suppress(asyncio.CancelledError, TransportClosed, Exception):
            await runner_task
        responder_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await responder_task
        with contextlib.suppress(Exception):
            await server.close()

    print("\n✓ stub-mode session ended cleanly")


# ── docker-mode entry point ────────────────────────────────────────────────


async def _run_real_docker_path() -> None:
    """End-to-end with the real DockerExecutor (requires Docker + image)."""
    from gg_relay.session.executor.docker import DockerExecutor

    image = os.environ.get("GG_RELAY_RUNNER_IMAGE", "gg-relay-runner:dev")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "✗ ANTHROPIC_API_KEY not set — required when DOCKER_AVAILABLE=true. "
            "Set DOCKER_AVAILABLE=false to run the stub-mode demo instead."
        )
        return

    socket_root = Path(
        os.environ.get("GG_RELAY_SOCKET_ROOT", "/tmp/gg-relay-demo")
    )
    socket_root.mkdir(parents=True, exist_ok=True)

    coord = HITLCoordinator()
    executor = DockerExecutor(image=image, socket_root=socket_root)
    spec = SessionSpec(
        prompt="say hi and exit",
        cwd=Path("/workspace"),
        plugins=PluginManifest(profile="minimal"),
        executor="docker",
    )
    runtime_ctx = SessionRuntimeContext(
        credentials={"ANTHROPIC_API_KEY": api_key},
        trace_id="demo-trace",
    )

    print(f"▶ [docker mode] image={image} socket_root={socket_root}")
    handle = await executor.start(spec, runtime_ctx=runtime_ctx)
    print(f"▶ runtime_id={handle.runtime_id}")

    bridge = WireBridge(transport=handle.transport, coordinator=coord)
    bridge_task = asyncio.create_task(bridge.run())
    responder_task = asyncio.create_task(_im_responder(coord))

    try:
        ended = await _drain_until_session_end(bridge, deadline_s=30.0)
        if not ended:
            print("◀ no session.end after 30s; aborting")
    finally:
        await bridge.shutdown()
        bridge_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bridge_task
        responder_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await responder_task
        await executor.stop(handle)
        await executor.close()

    print("\n✓ docker-mode session ended cleanly")


# ── main ───────────────────────────────────────────────────────────────────


async def main() -> None:
    docker_available = os.environ.get("DOCKER_AVAILABLE", "true").lower() in (
        "1",
        "true",
        "yes",
    )
    if docker_available:
        await _run_real_docker_path()
    else:
        await _run_stub_path()


if __name__ == "__main__":
    asyncio.run(main())
