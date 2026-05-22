"""Tests for make_sdk_runner(install_report=...) emission (Plan 2 Task 4)."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_code_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
)

from gg_relay.session.client import make_sdk_runner
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import DEFAULT_POLICY
from gg_relay.session.plugins import InstallReport
from gg_relay.session.spec import PluginManifest, SessionSpec
from gg_relay.session.transport.protocol import TransportClosed


def _spec(tmp_path: Path) -> SessionSpec:
    return SessionSpec(
        prompt="x",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
    )


class _StubClient:
    def __init__(self, options: Any) -> None:
        self.options = options

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        return None

    async def receive_messages(self) -> AsyncIterator[Any]:
        yield AssistantMessage(content=[TextBlock(text="hi")], model="stub")
        yield ResultMessage(
            subtype="success", duration_ms=0, duration_api_ms=0,
            is_error=False, num_turns=1, session_id="s",
            total_cost_usd=0.0, usage={},
        )


async def _drain(handle) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=1.0)
        except (TimeoutError, TransportClosed):
            break
        frames.append(dict(f))
        if f["type"] == "session.end":
            break
    return frames


async def test_install_report_emits_install_done_first(tmp_path: Path) -> None:
    report = InstallReport(
        schema_version="gg.install.v1",
        profile_id="minimal",
        selected_modules=("rules-core",),
        included_components=(),
        excluded_components=(),
        install_root=tmp_path / ".claude",
        installed_at="2026-05-22T00:00:00Z",
        duration_ms=42,
    )
    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=lambda opts: _StubClient(opts),
        install_report=report,
    )
    executor = InProcessExecutor(runner=runner)
    handle = await executor.start(_spec(tmp_path))
    frames = await _drain(handle)
    await executor.stop(handle)

    assert frames[0]["type"] == "install.done"
    assert frames[0]["seq"] == 0  # install.done is emitted before any SDK loop iter
    assert any(f["type"] == "session.end" for f in frames)


async def test_no_install_report_skips_install_done(tmp_path: Path) -> None:
    """install_report=None → no install.done frame (backward-compatible default)."""
    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=lambda opts: _StubClient(opts),
        # install_report omitted
    )
    executor = InProcessExecutor(runner=runner)
    handle = await executor.start(_spec(tmp_path))
    frames = await _drain(handle)
    await executor.stop(handle)

    assert all(f["type"] != "install.done" for f in frames)
    assert any(f["type"] == "session.end" for f in frames)


async def test_install_done_payload_matches_report(tmp_path: Path) -> None:
    report = InstallReport(
        schema_version="gg.install.v1",
        profile_id="python",
        selected_modules=("rules-python", "skills-python"),
        included_components=("lang:python",),
        excluded_components=(),
        install_root=tmp_path / "claude-root",
        installed_at="2026-05-22T11:30:00Z",
        duration_ms=1337,
    )
    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=lambda opts: _StubClient(opts),
        install_report=report,
    )
    executor = InProcessExecutor(runner=runner)
    handle = await executor.start(_spec(tmp_path))
    frames = await _drain(handle)
    await executor.stop(handle)

    done = next(f for f in frames if f["type"] == "install.done")
    assert done["profile_id"] == "python"
    assert done["modules"] == ["rules-python", "skills-python"]
    assert done["duration_ms"] == 1337
    assert done["install_root"] == str(tmp_path / "claude-root")
