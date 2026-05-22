"""ExecutorBackend Protocol — abstracts in-process vs. docker vs. (future) k8s."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from gg_relay.session.spec import RuntimeHandle, SessionSpec
from gg_relay.session.transport.inmemory import InMemoryTransport

RunnerFn = Callable[[InMemoryTransport, SessionSpec], Awaitable[None]]
"""Runner coroutine signature for InProcessExecutor.

Canonical home for the type alias (the executor's contract). Re-exported
from ``executor/inprocess.py`` and ``client.py`` for back-compat with
existing call sites.

CONTRACT (cooperative cancellation):
- The runner MUST have at least one ``await`` point so ``stop()``'s
  ``task.cancel()`` can land. A runner that does ``while True: pass`` will
  hang ``stop()`` indefinitely because asyncio can't preempt non-yielding
  coroutines.
- When the runner returns or raises, ``runner_wrapper.finally`` closes the
  runner-side transport, which propagates a close sentinel to the host side.
- For Task 8+ (real SDK runner): exceptions inside the runner should be
  surfaced to the host via an ``error`` event frame before re-raising, so
  the host can observe the root cause beyond just ``TransportClosed``.
"""


@runtime_checkable
class ExecutorBackend(Protocol):
    """Lifecycle: start() returns a ready-to-use RuntimeHandle holding a
    bidirectional transport. stop() tears down. health() probes liveness.

    The backend MUST NOT participate in event streaming; it only owns the
    runtime (container/coroutine/pod) and the transport handle.
    """

    async def start(self, spec: SessionSpec) -> RuntimeHandle: ...
    async def stop(self, handle: RuntimeHandle) -> None: ...
    async def health(self, handle: RuntimeHandle) -> bool: ...
