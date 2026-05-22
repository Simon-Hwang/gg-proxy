"""Real-API smoke test — hits Anthropic API ONCE (~$0.001).

Skipped unless ``ANTHROPIC_API_KEY`` is in the environment. Marked with
``requires_api_key`` so CI / default ``pytest -m "not requires_api_key"``
runs skip it cleanly.

Plan 2 Task 7 — companion to ``test_real_sdk_dispatch.py`` (stub-only).
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from gg_relay.session.client import make_sdk_runner
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.spec import PluginManifest, SessionSpec
from gg_relay.session.transport.protocol import TransportClosed

pytestmark = [pytest.mark.requires_api_key, pytest.mark.requires_sdk]


async def test_real_api_minimal_round_trip(tmp_path: Path) -> None:
    """One real Anthropic API call. Verifies dataclass dispatch end-to-end.

    Skipped unconditionally if ``ANTHROPIC_API_KEY`` is unset (so an
    accidental ``pytest -m requires_api_key`` on a CI machine without the
    key still passes via skip).
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set; skipping real API smoke")

    # Plain ToolPolicy — neutral tools auto-accept. Prompt asks for a
    # single-word reply, no tool use expected.
    policy = ToolPolicy()
    coordinator = HITLCoordinator()
    runner = make_sdk_runner(policy=policy, coordinator=coordinator)
    executor = InProcessExecutor(runner=runner)

    spec = SessionSpec(
        prompt="Reply with exactly the single word: OK",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
    )
    handle = await executor.start(spec)

    frames: list[dict[str, Any]] = []
    try:
        while True:
            try:
                f = await asyncio.wait_for(handle.transport.recv(), timeout=60.0)
            except (TimeoutError, TransportClosed):
                break
            frames.append(dict(f))
            if f["type"] == "session.end":
                break
    finally:
        await executor.stop(handle)

    types = [f["type"] for f in frames]
    assert "msg.chunk" in types, f"expected msg.chunk in {types}"
    end = next(f for f in frames if f["type"] == "session.end")
    assert end["status"] == "completed"
    # Tokens should be reported (at least input_tokens > 0)
    assert end["tokens"].get("input_tokens", 0) > 0
