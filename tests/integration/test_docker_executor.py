"""Integration test for the real Docker backend round-trip.

Skipped by default — requires both:
- a reachable Docker daemon (``@pytest.mark.requires_docker``)
- a real ``ANTHROPIC_API_KEY`` (``@pytest.mark.requires_api_key``)

When both are available, this exercises the entire docker path:
  1. ``DockerExecutor.start(spec, runtime_ctx)`` spins up a container from
     ``gg-relay-runner:dev`` (built locally via the spike script) with the
     unix socket bind-mounted in.
  2. ``WireBridge`` on the host drives the SDK conversation through the
     wire transport — receives install.done / msg.chunk / session.end.
  3. ``DockerExecutor.stop(handle)`` sends a shutdown frame and waits for
     the container to exit.

The test image is built once per session by the ``runner_image`` fixture;
if it already exists locally we reuse it. The build alone takes 5-10 min
in a cold cache, which is why this lives behind two markers.

Manual run:
    docker build -t gg-relay-runner:dev \\
        -f images/gg-relay-runner/Dockerfile \\
        --build-arg GG_PLUGINS_VERSION=v0.4.2 .
    ANTHROPIC_API_KEY=sk-... \\
        pytest tests/integration/test_docker_executor.py \\
        -v --no-cov -m "requires_docker and requires_api_key"
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from gg_relay.session.executor.docker import DockerExecutor
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.runner.bridge import WireBridge
from gg_relay.session.spec import (
    PluginManifest,
    SessionRuntimeContext,
    SessionSpec,
)

pytestmark = [
    pytest.mark.requires_docker,
    pytest.mark.requires_api_key,
]


@pytest.fixture(scope="module")
def docker_available() -> bool:
    """True iff the docker CLI can talk to a daemon."""
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "info"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


@pytest.fixture(scope="module")
def api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set")
    return key


@pytest.fixture(scope="module")
def runner_image(docker_available: bool) -> str:
    """The image tag DockerExecutor will pull/use.

    By default we use ``gg-relay-runner:dev`` (built locally via the spike
    script). Override with ``GG_RELAY_RUNNER_IMAGE=<tag>`` to point at a
    GHCR-pushed image instead.
    """
    if not docker_available:
        pytest.skip("docker daemon not reachable")
    return os.environ.get("GG_RELAY_RUNNER_IMAGE", "gg-relay-runner:dev")


@pytest.fixture
def socket_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Short host socket dir under /var/run (or /tmp) so AF_UNIX path stays
    under the 108-byte limit."""
    p = Path("/tmp/gg-relay-it")
    p.mkdir(exist_ok=True)
    return p


async def test_docker_executor_full_round_trip(
    tmp_path: Path,
    runner_image: str,
    api_key: str,
    socket_root: Path,
) -> None:
    """Smoke test: prompt → assistant response → session.end, all over a
    real container."""
    executor = DockerExecutor(
        image=runner_image,
        socket_root=socket_root,
        accept_timeout=30.0,
    )
    coordinator = HITLCoordinator()
    spec = SessionSpec(
        prompt="Reply with the exact word: OK",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="docker",
        timeout_s=60,
    )
    runtime_ctx = SessionRuntimeContext(
        credentials={"ANTHROPIC_API_KEY": api_key},
        trace_id="docker-it-trace",
        public_callback_base="http://localhost",
    )

    handle = None
    bridge = None
    bridge_task: asyncio.Task[None] | None = None
    try:
        handle = await executor.start(spec, runtime_ctx=runtime_ctx)
        bridge = WireBridge(
            handle.transport,
            coordinator,
            heartbeat_interval_s=5.0,
        )
        bridge_task = asyncio.create_task(bridge.run())
        # 120 s is generous — first-token latency on a cold image can be 30+ s.
        await asyncio.wait_for(bridge.wait_finished(), timeout=120.0)
    finally:
        if bridge is not None:
            with __import__("contextlib").suppress(Exception):
                await bridge.shutdown(grace=5.0)
        if bridge_task is not None:
            bridge_task.cancel()
            with __import__("contextlib").suppress(Exception):
                await bridge_task
        if handle is not None:
            await executor.stop(handle)
        await executor.close()

    types = [f["type"] for f in bridge.frames]
    assert "session.end" in types, f"no session.end seen; types={types}"
    # We expect at least one assistant chunk on the way to session.end.
    assert any(t == "msg.chunk" for t in types), (
        f"expected at least one msg.chunk before session.end; got {types}"
    )
