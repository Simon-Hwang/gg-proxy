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
from gg_relay.session.control import ControlChannel
from gg_relay.session.executor.protocol import ExecutorBackend
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.plugins.protocol import (
    InstallReport,
    PluginAssembler,
)
from gg_relay.session.runner.bridge import WireBridge
from gg_relay.session.runner.inprocess_control import InProcessBridge
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

ExecutorFactory = Callable[..., ExecutorBackend]
"""Factory signature: callable accepting ``(kind, policy, coordinator,
session_id, *, control_channel=None)`` and returning an :class:`ExecutorBackend`.

The trailing ``control_channel`` kwarg is new in Plan 6 D6.11 — production
wiring threads a fresh :class:`ControlChannel` per session through both
the executor (so an :class:`InProcessBridge` lands on
:attr:`RuntimeHandle.extra`) and the runner factory (so the runner's
:class:`ControlLoop` drains the same channel). Test factories that don't
care about pause/resume can ignore the kwarg — :class:`SessionManager`
always supplies it as a keyword so a 4-positional-only factory still
works as long as it accepts ``**kwargs`` or declares the param with a
default.
"""

_PauseBridge = WireBridge | InProcessBridge
"""Union of the two pause/resume bridge implementations the manager may
hold per session. Both expose ``async pause(reason=...)`` and
``async resume(hint=...)`` returning a :class:`ControlAck`."""


@dataclass(slots=True)
class _RunMetrics:
    """Per-session ephemeral counters surfaced via :attr:`SessionManager.metrics`.

    Plan 6 D6.12 adds the four aggregate fields the dashboard's global
    chart reads. ``_record_session_end`` (called from _persist_frame
    when a ``session.end`` arrives) populates them, then the _run
    finally block flushes the values to the store via
    :meth:`SessionRepository.update_session_aggregates`.
    """

    frames_persisted: int = 0
    frames_dropped: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    turn_count: int = 0


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


class SessionNotRunning(RuntimeError):
    """Raised by :meth:`SessionManager.pause` when the session is not
    currently in ``RUNNING`` state (e.g. it never finished queuing, or it
    already completed). Mapped to HTTP 409 at the API layer (Plan 6 Task 4).
    """


class SessionNotPaused(RuntimeError):
    """Raised by :meth:`SessionManager.resume` when the session is not in
    ``PAUSED`` state. Mapped to HTTP 409 at the API layer."""


class MaxPausedExceeded(RuntimeError):
    """Raised by :meth:`SessionManager.pause` when either the global
    ``max_paused`` or the per-API-key ``max_paused_per_api_key`` cap would
    be exceeded (Plan 6 D6.17). Mapped to HTTP 429 at the API layer with a
    ``Retry-After`` header derived from the smallest active paused-timeout.
    """


class ResumeQueueTimeout(RuntimeError):
    """Raised by :meth:`SessionManager.resume` when the semaphore can't
    be re-acquired within ``Config.resume_timeout_s`` after pause released
    the slot (Plan 6 D6.2 / §10 risk row). Maps to HTTP 429 with a
    Retry-After so the operator retries when other sessions finish."""


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
        paused_timeout_s: int = 1800,
        max_paused: int = 50,
        max_paused_per_api_key: int = 20,
        resume_timeout_s: float = 60.0,
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
        # ── Plan 6 D6.11/D6.2/D6.15/D6.17 pause/resume state ──────────────
        self._paused_timeout_s = paused_timeout_s
        self._max_paused = max_paused
        self._max_paused_per_api_key = max_paused_per_api_key
        self._resume_timeout_s = resume_timeout_s
        self._bridges: dict[str, _PauseBridge] = {}
        self._api_key_by_session: dict[str, str | None] = {}
        self._paused_set: set[str] = set()
        self._paused_at: dict[str, datetime] = {}
        self._paused_timers: dict[str, asyncio.Task[None]] = {}
        self._paused_holds_slot: set[str] = set()
        self._paused_by_key: dict[str, int] = {}

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
        api_key_id: str | None = None,
    ) -> str:
        """Enqueue a session for execution and return its id.

        Synchronously persists a row in ``queued`` state and spawns the
        background task. ``runtime_ctx.credentials`` are *never* persisted
        — they are passed through memory only to the executor.

        ``api_key_id`` is the per-tenant identifier used by pause()'s
        ``max_paused_per_api_key`` accounting (Plan 6 D6.17). The API
        layer derives it from the X-API-Key header; in-process callers
        may pass ``None``.
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
        self._api_key_by_session[sid] = api_key_id
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
        """Cancel a running, queued, or paused session.

        Cancels the background task (which transitions the row to
        ``cancelled``) and resolves every pending HITL request for the
        session as ``deny``. For paused sessions, also cancels the
        paused-timeout timer and releases pause bookkeeping (the
        semaphore slot was already released at pause time, so nothing
        to re-acquire). No-op if the session is not currently tracked.
        """
        self._cancel_paused_timer(sid)
        if sid in self._paused_set:
            self._release_pause_bookkeeping(sid)
        task = self._running_tasks.get(sid)
        if task is not None and not task.done():
            task.cancel()
        await self._coordinator.cancel_all(
            reason=f"cancel:{reason}", session_id=sid
        )

    async def pause(self, sid: str, *, reason: str | None = None) -> None:
        """Pause a running session (Plan 6 D6.1/D6.2/D6.11/D6.17).

        Sends a pause control frame through the per-session bridge, awaits
        the runner's ack (default 5 s — see
        :class:`~gg_relay.session.runner.bridge.BridgeAckTimeout`),
        transitions the row to ``PAUSED``, releases the semaphore slot
        so queued submits can proceed, and spawns a paused-timeout timer
        that will :meth:`cancel` the session if resume doesn't arrive
        within ``paused_timeout_s``.

        Raises:
          * :class:`SessionNotFound` — unknown id
          * :class:`SessionNotRunning` — not currently RUNNING
          * :class:`MaxPausedExceeded` — would exceed global or per-key cap
          * :class:`BridgeAckTimeout` — runner didn't ack in time
        """
        bridge = self._bridges.get(sid)
        if bridge is None:
            row = await self._store.get_session(sid)
            if row is None:
                raise SessionNotFound(sid)
            raise SessionNotRunning(
                f"session {sid} is {row['status']!r}; pause requires 'running'"
            )
        if sid in self._paused_set:
            # Idempotent: pausing an already-paused session is a no-op (but
            # we still re-arm the timer so the operator's intent is honoured).
            self._arm_paused_timer(sid)
            return
        self._check_paused_caps(sid)
        ack = await bridge.pause(reason=reason)
        if not ack.ok:
            raise RuntimeError(
                f"pause failed for session {sid}: {ack.error or 'unknown error'}"
            )
        self._paused_set.add(sid)
        self._paused_at[sid] = _utcnow()
        api_key = self._api_key_by_session.get(sid)
        if api_key is not None:
            self._paused_by_key[api_key] = self._paused_by_key.get(api_key, 0) + 1
        if sid in self._paused_holds_slot:
            # Defensive: pause was already accounted for in a prior call.
            pass
        else:
            self._paused_holds_slot.add(sid)
            self._sem.release()
        await self._store.update_session_status(
            sid, status=SessionState.PAUSED.value
        )
        await self._bus.publish(
            SessionStateChanged(
                session_id=sid,
                from_state=SessionState.RUNNING.value,
                to_state=SessionState.PAUSED.value,
                reason=reason,
            )
        )
        self._arm_paused_timer(sid)

    async def resume(self, sid: str, *, hint: str | None = None) -> None:
        """Resume a paused session (Plan 6 D6.2/D6.11).

        Re-acquires a semaphore slot (waiting up to ``resume_timeout_s``),
        sends the resume control frame, awaits the ack, transitions the
        row back to ``RUNNING``. The optional ``hint`` is delivered to
        the runner verbatim and may be used by the SDK
        ``client.query(hint)`` continuation.

        Raises:
          * :class:`SessionNotFound` — unknown id
          * :class:`SessionNotPaused` — not currently PAUSED
          * :class:`ResumeQueueTimeout` — semaphore couldn't be re-acquired
          * :class:`BridgeAckTimeout` — runner didn't ack
        """
        bridge = self._bridges.get(sid)
        if sid not in self._paused_set or bridge is None:
            row = await self._store.get_session(sid)
            if row is None:
                raise SessionNotFound(sid)
            raise SessionNotPaused(
                f"session {sid} is {row['status']!r}; resume requires 'paused'"
            )
        self._cancel_paused_timer(sid)
        try:
            await asyncio.wait_for(
                self._sem.acquire(), timeout=self._resume_timeout_s
            )
        except TimeoutError as exc:
            # Re-arm the timer so the paused-timeout still applies.
            self._arm_paused_timer(sid)
            raise ResumeQueueTimeout(
                f"resume timed out waiting {self._resume_timeout_s}s for slot"
            ) from exc
        try:
            ack = await bridge.resume(hint=hint)
        except BaseException:
            # Roll back the slot we just acquired before propagating.
            self._sem.release()
            self._arm_paused_timer(sid)
            raise
        if not ack.ok:
            self._sem.release()
            self._arm_paused_timer(sid)
            raise RuntimeError(
                f"resume failed for session {sid}: {ack.error or 'unknown error'}"
            )
        # Hand the slot back to _run's finally block via _paused_holds_slot.
        self._paused_holds_slot.discard(sid)
        self._release_pause_bookkeeping(sid, keep_holds=True)
        await self._store.update_session_status(
            sid, status=SessionState.RUNNING.value
        )
        await self._bus.publish(
            SessionStateChanged(
                session_id=sid,
                from_state=SessionState.PAUSED.value,
                to_state=SessionState.RUNNING.value,
                reason=hint,
            )
        )

    async def shutdown(
        self,
        *,
        grace_period_s: int | None = None,
        paused_action: Literal["cancel", "wait"] = "cancel",
    ) -> None:
        """C3 grace+drain (Plan 6 D6.15).

        Stops accepting new submits, then:
          * if ``paused_action='cancel'`` (default): cancels every paused
            session with ``reason='shutdown_during_pause'`` so the row
            settles deterministically.
          * if ``paused_action='wait'``: leaves paused sessions alone and
            relies on the same cancel-after-grace path used for running
            sessions.

        Waits up to ``grace_period_s`` for currently-running sessions to
        publish ``session.end``, then cancels the remainder. Always
        idempotent.
        """
        if not self._accepting_new and not self._running_tasks:
            return
        self._accepting_new = False
        if paused_action == "cancel":
            paused_ids = list(self._paused_set)
            for sid in paused_ids:
                with contextlib.suppress(Exception):
                    await self.cancel(sid, reason="shutdown_during_pause")
        grace = grace_period_s if grace_period_s is not None else self._grace_period_s
        deadline = asyncio.get_running_loop().time() + grace
        while self._running_tasks and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        for task in list(self._running_tasks.values()):
            if not task.done():
                task.cancel()
        results = await asyncio.gather(
            *self._running_tasks.values(), return_exceptions=True
        )
        for r in results:
            if isinstance(r, BaseException) and not isinstance(
                r, asyncio.CancelledError
            ):
                logger.warning("session task error during shutdown: %s", r)
        await self._coordinator.cancel_all(reason="shutdown")
        # Cancel any leftover paused timers (defensive — _run's finally
        # should have cleaned these already).
        for sid in list(self._paused_timers):
            self._cancel_paused_timer(sid)

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

        Plan 6 D6.2 note: the semaphore is acquired explicitly (rather
        than via ``async with self._sem``) because :meth:`pause` may
        release the slot mid-flight and :meth:`resume` re-acquires it.
        The :class:`set` ``_paused_holds_slot`` tracks whether THIS
        coroutine still owns the slot at finally time so we never
        over-release.
        """
        policy = self._effective_policy(spec)
        end_reason: str | None = None
        end_status: Literal[
            "completed", "failed", "cancelled", "interrupted"
        ] = "completed"
        acquired_slot = False
        try:
            await self._sem.acquire()
            acquired_slot = True
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
            # Release the slot if WE still hold it. If pause() released it
            # on our behalf the sid lives in _paused_holds_slot and we
            # must not double-release.
            if acquired_slot and sid not in self._paused_holds_slot:
                self._sem.release()
            self._cancel_paused_timer(sid)
            self._release_pause_bookkeeping(sid)
            self._bridges.pop(sid, None)
            self._api_key_by_session.pop(sid, None)
            await self._store.update_session_status(
                sid,
                status=end_status,
                ended_at=_utcnow(),
                end_reason=end_reason,
            )
            # Plan 6 D6.12: flush aggregates harvested from the
            # session.end frame. Always write — sessions that crashed
            # before session.end land with zeros, which is the correct
            # semantics for the dashboard.
            metrics = self._metrics.get(sid)
            if metrics is not None:
                with contextlib.suppress(Exception):
                    await self._store.update_session_aggregates(
                        sid,
                        input_tokens=metrics.input_tokens,
                        output_tokens=metrics.output_tokens,
                        cost_usd=metrics.cost_usd,
                        turn_count=metrics.turn_count,
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
        # Per-session control channel for pause/resume (Plan 6 D6.11).
        # Threaded into the executor factory as a kwarg so production
        # wiring can construct an InProcessBridge / share it with the
        # runner factory's ControlLoop. Test factories that don't care
        # may declare a `control_channel=None` kwarg or `**kwargs`.
        channel = ControlChannel()
        try:
            executor = self._executor_factory(
                spec.executor,
                policy,
                self._coordinator,
                sid,
                control_channel=channel,
            )
        except TypeError:
            # Backwards-compat for legacy 4-arg factories that don't
            # accept the kwarg yet — pause/resume will be unavailable
            # for those sessions but everything else continues to work.
            executor = self._executor_factory(
                spec.executor, policy, self._coordinator, sid
            )
        self._executors_in_flight[sid] = executor
        handle = await executor.start(spec, runtime_ctx=runtime_ctx)
        # For in-process, the executor exposes the bridge in handle.extra
        # (which is a tuple of (key, value) pairs — see :class:`RuntimeHandle`).
        for key, value in handle.extra:
            if key == "bridge" and isinstance(value, InProcessBridge):
                self._bridges[sid] = value
                break
        return handle

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
            self._bridges[sid] = bridge
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

    # ── pause/resume helpers ─────────────────────────────────────────

    def _check_paused_caps(self, sid: str) -> None:
        """Enforce ``max_paused`` (global) + ``max_paused_per_api_key``
        (Plan 6 D6.17). Raises :class:`MaxPausedExceeded` if either cap
        would be exceeded by pausing ``sid``.
        """
        if len(self._paused_set) >= self._max_paused:
            raise MaxPausedExceeded(
                f"global paused cap reached ({self._max_paused}); "
                "resume or cancel a paused session first"
            )
        api_key = self._api_key_by_session.get(sid)
        if api_key is None:
            return
        current = self._paused_by_key.get(api_key, 0)
        if current >= self._max_paused_per_api_key:
            raise MaxPausedExceeded(
                f"per-api-key paused cap reached ({self._max_paused_per_api_key}) "
                f"for key={api_key!r}"
            )

    def _arm_paused_timer(self, sid: str) -> None:
        self._cancel_paused_timer(sid)
        self._paused_timers[sid] = asyncio.create_task(
            self._paused_timeout_watchdog(sid), name=f"paused-timeout-{sid}"
        )

    def _cancel_paused_timer(self, sid: str) -> None:
        timer = self._paused_timers.pop(sid, None)
        if timer is not None and not timer.done():
            timer.cancel()

    async def _paused_timeout_watchdog(self, sid: str) -> None:
        try:
            await asyncio.sleep(self._paused_timeout_s)
        except asyncio.CancelledError:
            return
        # Timer fired — cancel the session deterministically. We do NOT
        # re-enter pause-bookkeeping cleanup here; cancel() handles it.
        if sid in self._paused_set:
            logger.info(
                "session %s exceeded paused_timeout_s=%s; cancelling",
                sid,
                self._paused_timeout_s,
            )
            with contextlib.suppress(Exception):
                await self.cancel(sid, reason="paused_timeout")

    def _release_pause_bookkeeping(
        self, sid: str, *, keep_holds: bool = False
    ) -> None:
        """Drop pause-related dict/set entries for ``sid`` without
        touching the semaphore. ``keep_holds=True`` is used by
        :meth:`resume` so the slot ownership transfer to ``_run`` isn't
        confused with a release.

        Only adjusts ``_paused_by_key`` if ``sid`` was *actually* in the
        paused set (so calls from ``_run``'s finally on never-paused
        sessions are no-ops).
        """
        was_paused = sid in self._paused_set
        self._paused_set.discard(sid)
        self._paused_at.pop(sid, None)
        if not keep_holds:
            self._paused_holds_slot.discard(sid)
        if not was_paused:
            return
        api_key = self._api_key_by_session.get(sid)
        if api_key is not None:
            remaining = self._paused_by_key.get(api_key, 0) - 1
            if remaining <= 0:
                self._paused_by_key.pop(api_key, None)
            else:
                self._paused_by_key[api_key] = remaining

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
        # Plan 6 D6.12: harvest per-session aggregates from session.end
        # before publishing — keeps the bookkeeping inside the
        # redacted-frame pipeline so manual end-frame injection in tests
        # also flows through.
        if redacted.get("type") == "session.end":
            self._record_session_end(sid, dict(redacted))
        typed = frame_to_event(sid, dict(redacted))
        if typed is not None:
            await self._bus.publish(typed)
        else:
            # Forward-compat: unknown wire frame types still reach legacy
            # str-topic subscribers (Plan 4 OTel subscriber etc.) until
            # they fully migrate. Plan 6+ removes this fallback.
            await self._bus.publish("frame", {"session_id": sid, **redacted})

    def _record_session_end(self, sid: str, frame: dict[str, Any]) -> None:
        """Plan 6 D6.12 — capture the session.end frame's token /
        cost / turn aggregates so ``_run``'s finally can flush them to
        the store. The actual DB write happens in :meth:`_run` so it
        can be awaited inside the existing transaction (avoids opening
        a fresh connection on every frame)."""
        metrics = self._metrics.get(sid)
        if metrics is None:
            return
        tokens = frame.get("tokens") or {}
        if isinstance(tokens, dict):
            metrics.input_tokens = int(tokens.get("input_tokens", 0) or 0)
            metrics.output_tokens = int(tokens.get("output_tokens", 0) or 0)
            metrics.turn_count = int(tokens.get("turn_count", 0) or 0)
        cost = frame.get("cost_usd")
        if isinstance(cost, int | float):
            metrics.cost_usd = float(cost)


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
    :class:`gg_relay.session.executor.protocol.RunnerFn`. The optional
    ``control_channel`` kwarg (Plan 6 D6.11) is threaded into the
    :class:`InProcessExecutor` so its ``handle.extra['bridge']`` is wired
    for pause/resume.
    """
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.executor.protocol import RunnerFn

    def _factory(
        kind: str,
        policy: ToolPolicy,
        coordinator: HITLCoordinator,
        session_id: str,
        *,
        control_channel: ControlChannel | None = None,
    ) -> ExecutorBackend:
        del kind
        runner: RunnerFn = runner_factory(policy, coordinator, session_id)
        return InProcessExecutor(runner=runner, control_channel=control_channel)

    return _factory
