"""SessionManager — Plan 4 D4.5, D4.9, D4.18, D4.19.

Receives :class:`SessionSpec` + :class:`SessionRuntimeContext`, persists a
queued row, spawns a background task that:

  1. Acquires a concurrency semaphore slot.
  2. Runs ``assembler.prepare(spec)`` to materialise plugins.
  3. Starts an :class:`ExecutorBackend` (in-process or docker).
  4. Drains the runtime transport, persisting + publishing every frame.
  5. Coordinates HITL via the shared :class:`HITLCoordinator`.
  6. Transitions session status to ``completed``/``failed``/``cancelled``
     and persists ``end_reason``.

A graceful :meth:`shutdown` stops accepting new submissions, waits up to
``grace_period_s`` for in-flight sessions to finish, then cancels the
remainder.

The manager intentionally accepts the executor as a factory (callable from
the spec's ``executor`` field) so the production lifespan can wire docker
+ inprocess without the manager needing to know about either class.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from gg_relay.core import (
    EventBus,
    InstallError,
    SessionCreated,
    SessionState,
    SessionStateChanged,
    SessionSummary,
    frame_to_event,
)
from gg_relay.redaction import RedactionEngine
from gg_relay.session.executor.protocol import ExecutorBackend
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.plugins.protocol import (
    InstallReport,
    PluginAssembler,
)
from gg_relay.session.runner.bridge import WireBridge
from gg_relay.session.spec import (
    RuntimeHandle,
    SessionRuntimeContext,
    SessionSpec,
)
from gg_relay.session.transport.protocol import (
    EventFrame,
    SessionTransport,
    TransportClosed,
)
from gg_relay.store import SessionRepository

logger = logging.getLogger("gg_relay.session.manager")

ExecutorFactory = Callable[[str, ToolPolicy, HITLCoordinator, str], ExecutorBackend]
"""Factory signature: ``(kind, policy, coordinator, session_id) -> ExecutorBackend``.

The lifespan creates one factory that closes over the docker / in-process
construction parameters and returns the appropriate backend per call.
Includes coordinator + policy so the inprocess runner can be wired without
the manager owning that knowledge.
"""


@dataclass(slots=True)
class _RunMetrics:
    """Per-session ephemeral counters surfaced via :attr:`SessionManager.metrics`."""

    frames_persisted: int = 0
    frames_dropped: int = 0


@dataclass(slots=True)
class SessionDetail:
    """Result of :meth:`SessionManager.get` — full row + frames page."""

    id: str
    status: SessionState
    spec_json: dict[str, Any]
    tags: tuple[str, ...]
    submitted_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    end_reason: str | None
    trace_id: str | None
    backend: str
    runtime_id: str | None
    frames: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# Single shared empty SessionRuntimeContext so ``submit()`` can have a
# concrete default without triggering ruff B008.
_DEFAULT_RUNTIME_CTX = SessionRuntimeContext()


class SessionNotFound(LookupError):
    """Raised by :meth:`SessionManager.get` / ``cancel`` when the id is unknown."""


class SessionManager:
    """Process-wide orchestrator for SessionSpec submissions.

    Construction is cheap; :meth:`submit` is the entry-point for the API
    layer. :meth:`shutdown` MUST be awaited at process shutdown to drain
    in-flight sessions.
    """

    def __init__(
        self,
        *,
        executor_factory: ExecutorFactory,
        assembler: PluginAssembler,
        store: SessionRepository,
        bus: EventBus,
        coordinator: HITLCoordinator,
        redactor: RedactionEngine,
        default_policy: ToolPolicy,
        install_dir_root: Path,
        default_timeout_s: int = 1800,
        max_concurrent: int = 10,
        grace_period_s: int = 30,
    ) -> None:
        self._executor_factory = executor_factory
        self._assembler = assembler
        self._store = store
        self._bus = bus
        self._coordinator = coordinator
        self._redactor = redactor
        self._default_policy = default_policy
        self._install_dir_root = install_dir_root
        self._default_timeout_s = default_timeout_s
        self._grace_period_s = grace_period_s
        self._sem = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._accepting_new = True
        self._metrics: dict[str, _RunMetrics] = {}
        # Per-session executor handle kept here so _run's finally can stop()
        # the right backend even if _drive_session raises mid-flight.
        self._executors_in_flight: dict[str, ExecutorBackend] = {}

    @property
    def accepting_new(self) -> bool:
        return self._accepting_new

    @property
    def running_session_count(self) -> int:
        return len(self._running_tasks)

    @property
    def metrics(self) -> Mapping[str, _RunMetrics]:
        return self._metrics

    # ── public API ─────────────────────────────────────────────────────

    async def submit(
        self,
        spec: SessionSpec,
        *,
        runtime_ctx: SessionRuntimeContext = _DEFAULT_RUNTIME_CTX,
    ) -> str:
        """Enqueue a session for execution and return its id.

        Synchronously persists a row in ``queued`` state and spawns the
        background task. ``runtime_ctx.credentials`` are *never* persisted
        — they are passed through memory only to the executor.
        """
        if not self._accepting_new:
            raise RuntimeError("SessionManager is shutting down; refusing new submit")

        sid = uuid.uuid4().hex
        spec_redacted = self._redactor.redact_dict(spec.to_json_safe())
        await self._store.create_session(
            id=sid,
            spec_json=spec_redacted,
            trace_id=runtime_ctx.trace_id or None,
            backend=spec.executor,
            tags=tuple(spec.tags),
        )
        await self._bus.publish(
            SessionCreated(
                session_id=sid,
                prompt_redacted=str(spec_redacted.get("prompt", ""))[:512],
                tags=tuple(spec.tags),
            )
        )
        self._metrics[sid] = _RunMetrics()
        task = asyncio.create_task(
            self._run(sid, spec, runtime_ctx), name=f"session-{sid}"
        )
        self._running_tasks[sid] = task
        task.add_done_callback(self._on_task_done(sid))
        return sid

    def _on_task_done(self, sid: str) -> Callable[[asyncio.Task[None]], None]:
        def _cb(task: asyncio.Task[None]) -> None:
            self._running_tasks.pop(sid, None)

        return _cb

    async def list(
        self,
        *,
        status: SessionState | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionSummary]:
        rows = await self._store.list_sessions(
            status=status.value if status else None,
            tag=tag,
            limit=limit,
            offset=offset,
        )
        return [
            SessionSummary(
                id=r["id"],
                status=SessionState(r["status"]),
                submitted_at=r["submitted_at"],
                started_at=r["started_at"],
                ended_at=r["ended_at"],
                tags=tuple(r["tags"] or ()),
                backend=r["backend"],
                end_reason=r["end_reason"],
            )
            for r in rows
        ]

    async def get(
        self, sid: str, *, frames_limit: int = 100, frames_offset: int = 0
    ) -> SessionDetail:
        row = await self._store.get_session(sid)
        if row is None:
            raise SessionNotFound(sid)
        frames_rows = await self._store.list_frames(
            sid, limit=frames_limit, offset=frames_offset
        )
        return SessionDetail(
            id=row["id"],
            status=SessionState(row["status"]),
            spec_json=dict(row["spec_json"] or {}),
            tags=tuple(row["tags"] or ()),
            submitted_at=row["submitted_at"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            end_reason=row["end_reason"],
            trace_id=row["trace_id"],
            backend=row["backend"],
            runtime_id=row["runtime_id"],
            frames=tuple(dict(r) for r in frames_rows),
        )

    async def cancel(self, sid: str, *, reason: str = "user_request") -> None:
        """Cancel a running or queued session.

        Cancels the background task (which transitions the row to
        ``cancelled``) and resolves every pending HITL request for the
        session as ``deny``. No-op if the session is not currently
        tracked — the caller might be cancelling something already done.
        """
        task = self._running_tasks.get(sid)
        if task is not None and not task.done():
            task.cancel()
        await self._coordinator.cancel_all(
            reason=f"cancel:{reason}", session_id=sid
        )

    async def shutdown(self, *, grace_period_s: int | None = None) -> None:
        """C3 grace+drain.

        Stops accepting new submits, waits up to ``grace_period_s`` for
        currently-running sessions to publish ``session.end``, then cancels
        the remainder. Always idempotent.
        """
        if not self._accepting_new and not self._running_tasks:
            return
        self._accepting_new = False
        grace = grace_period_s if grace_period_s is not None else self._grace_period_s
        deadline = asyncio.get_running_loop().time() + grace
        while self._running_tasks and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        for task in list(self._running_tasks.values()):
            if not task.done():
                task.cancel()
        # Await all so exception groups don't leak.
        results = await asyncio.gather(
            *self._running_tasks.values(), return_exceptions=True
        )
        for r in results:
            if isinstance(r, BaseException) and not isinstance(
                r, asyncio.CancelledError
            ):
                logger.warning("session task error during shutdown: %s", r)
        await self._coordinator.cancel_all(reason="shutdown")

    # ── internal ─────────────────────────────────────────────────────

    async def _run(
        self,
        sid: str,
        spec: SessionSpec,
        runtime_ctx: SessionRuntimeContext,
    ) -> None:
        """The per-session lifecycle.

        Wrapped to capture every reachable failure mode and persist a
        meaningful ``end_reason``.
        """
        policy = self._effective_policy(spec)
        end_reason: str | None = None
        end_status: Literal[
            "completed", "failed", "cancelled", "interrupted"
        ] = "completed"
        try:
            async with self._sem:
                await self._store.update_session_status(
                    sid, status=SessionState.RUNNING.value, started_at=_utcnow()
                )
                await self._bus.publish(
                    SessionStateChanged(
                        session_id=sid,
                        from_state=SessionState.QUEUED.value,
                        to_state=SessionState.RUNNING.value,
                    )
                )
                install_report = await self._prepare_plugins(sid, spec)
                handle = await self._start_executor(sid, spec, runtime_ctx, policy)
                try:
                    await self._store.update_session_status(
                        sid, runtime_id=handle.runtime_id
                    )
                    timeout = spec.timeout_s or self._default_timeout_s
                    async with asyncio.timeout(timeout):
                        await self._drive_session(sid, spec, handle, install_report)
                finally:
                    with contextlib.suppress(Exception):
                        executor = self._executors_in_flight.pop(sid, None)
                        if executor is not None:
                            await executor.stop(handle)
        except TimeoutError:
            end_status = "cancelled"
            end_reason = "timeout"
            await self._coordinator.cancel_all(
                reason="timeout", session_id=sid
            )
        except asyncio.CancelledError:
            end_status = "cancelled"
            end_reason = "cancelled"
            raise
        except _PluginInstallSummary as exc:
            end_status = "failed"
            end_reason = f"install:{exc.code}"
            await self._bus.publish(
                InstallError(
                    session_id=sid,
                    code="install_failed",
                    message=str(exc)[:128],
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            end_status = "failed"
            end_reason = f"{type(exc).__name__}:{str(exc)[:96]}"
            logger.exception("session %s failed", sid)
            await self._bus.publish(
                InstallError(
                    session_id=sid,
                    code=type(exc).__name__,
                    message=str(exc)[:512],
                )
            )
        finally:
            await self._store.update_session_status(
                sid,
                status=end_status,
                ended_at=_utcnow(),
                end_reason=end_reason,
            )
            await self._bus.publish(
                SessionStateChanged(
                    session_id=sid,
                    from_state=SessionState.RUNNING.value,
                    to_state=end_status,
                    reason=end_reason,
                )
            )

    def _effective_policy(self, spec: SessionSpec) -> ToolPolicy:
        override = spec.hitl_policy
        if isinstance(override, ToolPolicy):
            return override
        return self._default_policy

    async def _prepare_plugins(
        self, sid: str, spec: SessionSpec
    ) -> InstallReport | None:
        """Run the assembler; raise :class:`_PluginInstallSummary` on failure."""
        install_dir = self._install_dir_root / sid
        try:
            report = await self._assembler.prepare(spec, install_dir=install_dir)
        except Exception as exc:
            raise _PluginInstallSummary(type(exc).__name__, str(exc)) from exc
        return report

    async def _start_executor(
        self,
        sid: str,
        spec: SessionSpec,
        runtime_ctx: SessionRuntimeContext,
        policy: ToolPolicy,
    ) -> RuntimeHandle:
        executor = self._executor_factory(
            spec.executor, policy, self._coordinator, sid
        )
        self._executors_in_flight[sid] = executor
        return await executor.start(spec, runtime_ctx=runtime_ctx)

    async def _drive_session(
        self,
        sid: str,
        spec: SessionSpec,
        handle: RuntimeHandle,
        install_report: InstallReport | None,
    ) -> None:
        """Drain the transport. Docker uses WireBridge, in-process drains
        the transport directly."""
        if spec.executor == "docker":
            bridge = WireBridge(handle.transport, self._coordinator)
            bridge_task = asyncio.create_task(
                bridge.run(), name=f"bridge-{sid}"
            )
            try:
                await bridge_task
            finally:
                await bridge.shutdown(grace=5.0)
                # Drain whatever the bridge buffered into the store + bus.
                await self._persist_frames(sid, bridge.frames)
        else:
            await self._drain_inprocess_transport(sid, handle.transport)
        del install_report  # currently unused; reserved for Plan 4+ persistence

    async def _drain_inprocess_transport(
        self, sid: str, transport: SessionTransport
    ) -> None:
        """Receive frames from the in-process transport until close.

        Each frame is redacted, persisted, and published. Stops when the
        runner closes the transport OR a ``session.end`` frame arrives
        (defence-in-depth — the close should follow naturally).
        """
        while True:
            try:
                frame = await transport.recv()
            except TransportClosed:
                return
            await self._persist_frame(sid, frame)
            if frame.get("type") == "session.end":
                return

    async def _persist_frames(
        self, sid: str, frames: Sequence[EventFrame]
    ) -> None:
        for f in frames:
            await self._persist_frame(sid, f)

    async def _persist_frame(self, sid: str, frame: Mapping[str, Any]) -> None:
        """Redact, persist, publish — the per-frame pipeline.

        The typed-event publish (Plan 5 D5.2=A3) replaces the legacy
        ``bus.publish("frame", dict)`` fan-out. Frames whose type has no
        registered factory in ``_FRAME_TO_EVENT`` fall back to a legacy
        string-topic publish so future wire-protocol additions don't
        silently disappear from subscribers that still match on ``"frame"``.
        """
        redacted = self._redactor.redact_frame(frame)
        ts_str = redacted.get("ts")
        ts = _utcnow()
        if isinstance(ts_str, str):
            with contextlib.suppress(ValueError):
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        try:
            await self._store.append_frame(
                sid,
                seq=int(redacted.get("seq", 0)),
                ts=ts,
                type_=str(redacted.get("type", "unknown")),
                payload=dict(redacted),
            )
            metrics = self._metrics.get(sid)
            if metrics is not None:
                metrics.frames_persisted += 1
        except Exception as exc:
            logger.warning(
                "frame persistence failed sid=%s type=%s err=%s",
                sid,
                redacted.get("type"),
                exc,
            )
            metrics = self._metrics.get(sid)
            if metrics is not None:
                metrics.frames_dropped += 1
        typed = frame_to_event(sid, dict(redacted))
        if typed is not None:
            await self._bus.publish(typed)
        else:
            # Forward-compat: unknown wire frame types still reach legacy
            # str-topic subscribers (Plan 4 OTel subscriber etc.) until
            # they fully migrate. Plan 6+ removes this fallback.
            await self._bus.publish("frame", {"session_id": sid, **redacted})


class _PluginInstallSummary(Exception):
    """Internal: lift PluginInstallError + arbitrary assembler errors to a
    uniform shape so ``_run``'s exception ladder stays linear."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# Convenience factory for the common in-process wiring used by tests.
def make_inprocess_factory(
    runner_factory: Callable[
        [ToolPolicy, HITLCoordinator, str],
        Callable[..., Awaitable[None]],
    ],
) -> ExecutorFactory:
    """Build an :data:`ExecutorFactory` that always returns
    :class:`InProcessExecutor` wired with the supplied ``runner_factory``.

    ``runner_factory(policy, coordinator, session_id)`` MUST return a runner
    coroutine factory compatible with
    :class:`gg_relay.session.executor.protocol.RunnerFn`.
    """
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.executor.protocol import RunnerFn

    def _factory(
        kind: str,
        policy: ToolPolicy,
        coordinator: HITLCoordinator,
        session_id: str,
    ) -> ExecutorBackend:
        del kind
        runner: RunnerFn = runner_factory(policy, coordinator, session_id)
        return InProcessExecutor(runner=runner)

    return _factory
