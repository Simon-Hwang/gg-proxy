"""trace_id → ClaudeCodeOptions.env injection — Plan 7 D7.19 / Task 14.

Confirms the in-process SDK runner threads
``SessionRuntimeContext.trace_id`` into the SDK's environment
variable ``RELAY_TRACE_ID`` (mirroring :class:`DockerExecutor` so the
docker + inprocess backends emit the same env contract).

We capture the constructed :class:`ClaudeCodeOptions` by stubbing the
SDK factory; the runner returns immediately once the stub yields a
``ResultMessage`` so the test doesn't need a live SDK.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_code_sdk import ResultMessage

from gg_relay.session.client import make_sdk_runner
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import DEFAULT_POLICY
from gg_relay.session.spec import (
    PluginManifest,
    SessionRuntimeContext,
    SessionSpec,
)
from gg_relay.session.transport.protocol import TransportClosed

pytestmark = pytest.mark.asyncio


class _CapturingStub:
    """SDK stub that records the :class:`ClaudeCodeOptions` it was given.

    Yields a single ``ResultMessage`` so the runner core exits cleanly
    after one loop iteration; that's enough to capture the options
    object the runner built.
    """

    captured: Any = None

    def __init__(self, options: Any) -> None:
        type(self).captured = options

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def receive_messages(self) -> AsyncIterator[Any]:
        # ``usage=None`` is fine — the runner core defaults to {} in
        # the session.end frame and the test doesn't read tokens.
        msg = ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="x",
            usage={},
            total_cost_usd=0.0,
            result="ok",
            permission_denials=[],
        )
        yield msg


def _spec(tmp_path: Path) -> SessionSpec:
    return SessionSpec(
        prompt="x",
        cwd=tmp_path,
        plugins=PluginManifest(
            profile="minimal",
            extra_env=(("FOO", "bar"),),
        ),
        executor="inprocess",
    )


async def _drain_to_end(handle, *, timeout: float = 1.0) -> None:
    """Block until session.end (or transport close) so the runner exits."""
    import asyncio

    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=timeout)
        except (TimeoutError, TransportClosed):
            return
        if f.get("type") == "session.end":
            return


async def _run_once(
    tmp_path: Path,
    *,
    runtime_ctx: SessionRuntimeContext | None = None,
) -> None:
    """Spin up the executor + runner one time, drain, then stop."""
    _CapturingStub.captured = None
    coord = HITLCoordinator()
    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=coord,
        sdk_factory=_CapturingStub,
        runtime_ctx=runtime_ctx,
    )
    executor = InProcessExecutor(runner=runner)
    handle = await executor.start(_spec(tmp_path))
    try:
        await _drain_to_end(handle)
    finally:
        await executor.stop(handle)


class TestTraceIdInjection:
    async def test_trace_id_injected_into_env(self, tmp_path: Path) -> None:
        """``runtime_ctx.trace_id`` lands in ``ClaudeCodeOptions.env``."""
        await _run_once(
            tmp_path,
            runtime_ctx=SessionRuntimeContext(trace_id="abc123"),
        )
        opts = _CapturingStub.captured
        assert opts is not None
        env = dict(opts.env or {})
        assert env.get("RELAY_TRACE_ID") == "abc123"
        # extra_env still flows through.
        assert env.get("FOO") == "bar"

    async def test_no_trace_id_means_no_env_key(self, tmp_path: Path) -> None:
        """Empty / missing trace_id → ``RELAY_TRACE_ID`` absent from env."""
        await _run_once(
            tmp_path,
            runtime_ctx=SessionRuntimeContext(),  # trace_id=""
        )
        opts = _CapturingStub.captured
        assert opts is not None
        env = dict(opts.env or {})
        assert "RELAY_TRACE_ID" not in env
        # extra_env still flows through.
        assert env.get("FOO") == "bar"

    async def test_no_runtime_ctx_means_no_env_key(self, tmp_path: Path) -> None:
        """Calling ``make_sdk_runner`` without ``runtime_ctx`` is also safe."""
        await _run_once(tmp_path, runtime_ctx=None)
        opts = _CapturingStub.captured
        assert opts is not None
        env = dict(opts.env or {})
        assert "RELAY_TRACE_ID" not in env

    async def test_trace_id_does_not_clobber_existing_env(
        self, tmp_path: Path
    ) -> None:
        """A ``RELAY_TRACE_ID`` from ``extra_env`` is overridden by runtime_ctx.

        Reasoning: ``extra_env`` is operator-supplied per-spec, but
        the runtime trace_id reflects the actual OTel context for
        THIS submission. The runtime-context value MUST win otherwise
        observability data would be wrong.
        """
        spec_with_env = SessionSpec(
            prompt="x",
            cwd=tmp_path,
            plugins=PluginManifest(
                profile="minimal",
                extra_env=(("RELAY_TRACE_ID", "from-extra-env"),),
            ),
            executor="inprocess",
        )
        _CapturingStub.captured = None
        coord = HITLCoordinator()
        runner = make_sdk_runner(
            policy=DEFAULT_POLICY,
            coordinator=coord,
            sdk_factory=_CapturingStub,
            runtime_ctx=SessionRuntimeContext(trace_id="from-runtime"),
        )
        executor = InProcessExecutor(runner=runner)
        handle = await executor.start(spec_with_env)
        try:
            await _drain_to_end(handle)
        finally:
            await executor.stop(handle)
        opts = _CapturingStub.captured
        env = dict(opts.env or {})
        assert env.get("RELAY_TRACE_ID") == "from-runtime"
