"""InProcessExecutor — spawn runner coroutine in the same event loop.

The runner callable receives (runner_side_transport, spec) and is responsible
for driving the SDK (or stubbed equivalent). When the runner returns, the
runner-side transport is closed automatically.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime

from gg_relay.session.executor.protocol import RunnerFn
from gg_relay.session.spec import RuntimeHandle, SessionRuntimeContext, SessionSpec
from gg_relay.session.transport.inmemory import make_pair

# Re-exported so existing `from gg_relay.session.executor.inprocess import RunnerFn`
# call sites keep working. Canonical definition lives in executor/protocol.py.
__all__ = ["InProcessExecutor", "RunnerFn"]

# Module-level sentinel so the default argument is a single frozen instance
# (avoids ruff B008 and matches the "frozen+slots is safe to share" promise of
# SessionRuntimeContext).
_DEFAULT_RUNTIME_CTX = SessionRuntimeContext()


class InProcessExecutor:
    """Runs the runner callable as an asyncio task in the same event loop."""

    def __init__(self, runner: RunnerFn) -> None:
        self._runner = runner
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def start(
        self,
        spec: SessionSpec,
        *,
        runtime_ctx: SessionRuntimeContext = _DEFAULT_RUNTIME_CTX,
    ) -> RuntimeHandle:
        # runtime_ctx is accepted for ExecutorBackend Protocol parity with
        # DockerExecutor (Plan 3 D3.16). The in-process backend ignores
        # credentials/trace_id because the runner shares the host process and
        # already inherits all env / OTel context. Tests can construct
        # InProcessExecutor without ever passing runtime_ctx.
        del runtime_ctx
        host_side, runner_side = make_pair()
        runtime_id = uuid.uuid4().hex

        async def runner_wrapper() -> None:
            try:
                await self._runner(runner_side, spec)
            finally:
                await runner_side.close()

        task = asyncio.create_task(runner_wrapper(), name=f"runner-{runtime_id}")
        self._tasks[runtime_id] = task
        # Auto-drop entry on natural completion so _tasks doesn't grow unbounded
        # when SessionManager (Task 10+) drives sessions whose normal exit isn't
        # paired with a stop() call. stop() also pops; pop(default) is idempotent.
        task.add_done_callback(lambda _t: self._tasks.pop(runtime_id, None))

        return RuntimeHandle(
            backend="inprocess",
            runtime_id=runtime_id,
            transport=host_side,
            started_at=datetime.now(UTC),
        )

    async def stop(self, handle: RuntimeHandle) -> None:
        task = self._tasks.pop(handle.runtime_id, None)
        if task is not None and not task.done():
            task.cancel()
            # Swallow CancelledError + any runner exception; we are tearing
            # down. Runner failures surface to the host side via TransportClosed
            # on the next recv() (runner_wrapper closes the transport in finally).
            # NOT BaseException — SystemExit / KeyboardInterrupt must propagate.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await handle.transport.close()

    async def health(self, handle: RuntimeHandle) -> bool:
        return handle.transport.is_alive
