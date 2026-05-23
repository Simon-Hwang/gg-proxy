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
import random
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
    classify_sdk_error,
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
    PluginManifest,
    RuntimeHandle,
    SessionRuntimeContext,
    SessionSpec,
)
from gg_relay.session.transport.protocol import (
    EventFrame,
    SessionTransport,
    TransportClosed,
)
from gg_relay.store import ConcurrencyError, SessionRepository

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
    # Plan 7 Task 6b / D7.26 — single-team multi-maintainer collaboration.
    # ``owner`` is auto-attributed from the API key label at submit time;
    # ``description`` is a short free-form annotation truncated to 512
    # chars by the router. Both may be ``None`` for sessions submitted
    # before Plan 7 Task 6b landed.
    owner: str | None = None
    description: str | None = None
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


# Plan 7 D7.5 / Task 8 — bounded jitter for the optimistic-locking
# retry. Kept small (≤ 50ms) so a single retry adds at most ~50ms to
# the pause/resume latency even when contended.
_RETRY_JITTER_MAX_S = 0.05


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
        audit_service: Any = None,
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
        # Plan 8 D8.4 / Task 5 — durable audit log. Optional so legacy
        # in-process callers (existing unit tests, future programmatic
        # clients) keep working without an audit sink. When ``None``
        # the explicit audit hooks below skip silently — the fallback
        # middleware still picks up the mutation in the API path.
        self._audit = audit_service
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
        # Plan 8 D8.4 / Task 5 — per-session owner label tracked in
        # memory so cancel/pause/resume can attribute audit rows
        # without a DB round-trip. Populated at submit; cleaned up by
        # ``_run``'s finally alongside the rest of the per-session
        # state. ``None`` means "API key had no label" — falls back to
        # ``"anon"`` at audit time.
        self._owner_by_session: dict[str, str | None] = {}

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
        owner: str | None = None,
        description: str | None = None,
        parent_session_id: str | None = None,
    ) -> str:
        """Enqueue a session for execution and return its id.

        Synchronously persists a row in ``queued`` state and spawns the
        background task. ``runtime_ctx.credentials`` are *never* persisted
        — they are passed through memory only to the executor.

        ``api_key_id`` is the per-tenant identifier used by pause()'s
        ``max_paused_per_api_key`` accounting (Plan 6 D6.17). The API
        layer derives it from the X-API-Key header; in-process callers
        may pass ``None``.

        Plan 7 Task 6b / D7.26 — ``owner`` and ``description`` are
        forwarded verbatim to :meth:`SessionStore.create_session`.
        The manager intentionally does **not** read
        ``request.state.api_key_label`` itself — the router is
        responsible for collapsing
        ``req.owner or request.state.api_key_label or 'anon'`` and
        passing the resolved string here. Keeping the manager
        framework-agnostic preserves the in-process call sites used
        by tests and future programmatic clients.

        Plan 8 D8.6 / Task 9 — ``parent_session_id`` (optional) marks
        the new row as the retry of an earlier session. Forwarded
        verbatim to :meth:`SessionStore.create_session`; ``None``
        leaves the column NULL (top-level submission). Set by
        :meth:`retry`; client-facing endpoints do not accept it.
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
            owner=owner,
            description=description,
            parent_session_id=parent_session_id,
        )
        # Plan 8 D8.4 / Task 5 — explicit audit row for every session
        # creation. NOT yet inside the same transaction as
        # ``create_session`` (the store doesn't accept an external
        # ``conn`` for that path); a v2.5 polish task will unify the
        # two writes. Until then we accept a tiny window where the
        # session row exists without an audit row (mutation succeeded
        # but audit insert raced into a transient DB error). The
        # fallback middleware does NOT cover this case (it skips
        # ``unknown_mutation`` on success because the explicit hook
        # already ran for the typical happy path).
        await self._audit_record(
            actor=owner or "anon",
            action="session_create",
            target_type="session",
            target_id=sid,
            metadata={
                "backend": spec.executor,
                "tags": list(spec.tags),
                "trace_id": runtime_ctx.trace_id or None,
                "has_description": description is not None,
                "prompt_len": len(str(spec_redacted.get("prompt", ""))),
                "parent_session_id": parent_session_id,
            },
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
        self._owner_by_session[sid] = owner
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
        after: str | None = None,
    ) -> tuple[list[SessionSummary], str | None]:
        """List sessions newest-first with cursor pagination.

        Plan 7 D7.6 / Task 9. Thin wrapper around
        :meth:`SessionStore.list_sessions` that converts the rows to
        immutable :class:`SessionSummary` instances and forwards the
        cursor through unchanged. Returns ``(summaries, next_cursor)``;
        ``next_cursor`` is ``None`` once the result set is exhausted.
        """
        rows, next_cursor = await self._store.list_sessions(
            status=status.value if status else None,
            tag=tag,
            limit=limit,
            after=after,
        )
        summaries = [
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
        return summaries, next_cursor

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
            owner=row.get("owner"),
            description=row.get("description"),
            frames=tuple(dict(r) for r in frames_rows),
        )

    async def retry(
        self, sid: str, *, actor: str | None = None
    ) -> str:
        """Submit a fresh session reusing the original's spec, return its id.

        Plan 8 D8.6 / Task 9. Reads ``sessions.spec_json`` for ``sid``,
        rebuilds a :class:`SessionSpec` (credentials are NOT persisted,
        so the new submission runs without them — the executor must
        either be ``inprocess`` or accept its credentials elsewhere)
        and forwards to :meth:`submit` with ``parent_session_id=sid``.
        The retry chain therefore lives entirely in the
        ``sessions.parent_session_id`` column; no separate "retry"
        table is needed.

        ``actor`` (the API key label of the user requesting the retry)
        is used for both the ``owner`` of the new session AND the
        audit row's actor. When ``actor`` is ``None`` we fall back to
        the original session's owner so an admin's batch retry of
        someone else's session keeps the original attribution. The
        audit row records ``parent_session_id`` in metadata so the
        dashboard's audit timeline can render the retry edge.

        Raises:
            :class:`SessionNotFound` — ``sid`` does not exist.
            :class:`gg_relay.core.RetryConfigError` — original spec
                is missing fields required to reconstruct a viable
                :class:`SessionSpec` (no prompt, no plugins).
        """
        original = await self._store.get_session(sid)
        if original is None:
            raise SessionNotFound(sid)

        # Reconstruct the SessionSpec from the redacted spec_json. The
        # plugins manifest is required (PluginManifest.__post_init__
        # raises if profile/modules/skills are all empty), so we
        # surface a RetryConfigError BEFORE calling submit() — that
        # keeps the failure path symmetric with the "no prompt" case
        # and avoids landing a half-baked sessions row.
        spec_json = dict(original.get("spec_json") or {})
        prompt = str(spec_json.get("prompt") or "")
        if not prompt:
            from gg_relay.core import RetryConfigError

            raise RetryConfigError(
                f"cannot retry session {sid}: persisted spec has no prompt"
            )

        plugins_data = spec_json.get("plugins") or {}
        if not isinstance(plugins_data, Mapping):
            plugins_data = {}
        try:
            plugins = PluginManifest(
                profile=plugins_data.get("profile"),
                modules=tuple(plugins_data.get("modules") or ()),
                skills=tuple(plugins_data.get("skills") or ()),
                with_components=tuple(plugins_data.get("with_components") or ()),
                without_components=tuple(
                    plugins_data.get("without_components") or ()
                ),
                extra_env=tuple(
                    (k, v) for k, v in (plugins_data.get("extra_env") or [])
                ),
            )
        except ValueError as exc:
            from gg_relay.core import RetryConfigError

            raise RetryConfigError(
                f"cannot retry session {sid}: {exc}"
            ) from exc

        retry_spec = SessionSpec(
            prompt=prompt,
            cwd=Path(str(spec_json.get("cwd") or ".")),
            plugins=plugins,
            executor=str(spec_json.get("executor") or "inprocess"),  # type: ignore[arg-type]
            timeout_s=int(spec_json.get("timeout_s") or self._default_timeout_s),
            tags=tuple(spec_json.get("tags") or ()),
        )

        owner = actor or original.get("owner") or "anon"
        original_desc = original.get("description")
        new_description = (
            original_desc if original_desc else f"Retry of {sid}"
        )
        new_sid = await self.submit(
            retry_spec,
            owner=owner,
            description=new_description,
            parent_session_id=sid,
        )
        await self._audit_record(
            actor=owner,
            action="session_retry",
            target_type="session",
            target_id=new_sid,
            metadata={
                "parent_session_id": sid,
                "tags": list(retry_spec.tags),
                "backend": retry_spec.executor,
            },
        )
        return new_sid

    async def cancel(self, sid: str, *, reason: str = "user_request") -> None:
        """Cancel a running, queued, or paused session.

        Cancels the background task (which transitions the row to
        ``cancelled``) and resolves every pending HITL request for the
        session as ``deny``. For paused sessions, also cancels the
        paused-timeout timer and releases pause bookkeeping (the
        semaphore slot was already released at pause time, so nothing
        to re-acquire). No-op if the session is not currently tracked.

        Plan 8 D8.4 / Task 5 — emits a ``session_cancel`` audit row
        before tearing down so the audit trail captures the operator
        intent even if the cleanup path raises.
        """
        await self._audit_record(
            actor=self._owner_of(sid),
            action="session_cancel",
            target_type="session",
            target_id=sid,
            metadata={"reason": reason},
        )
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

        Plan 7 D7.5 / Task 8 — the row transition uses optimistic
        locking via :meth:`_update_status_locked` (read version,
        attempt UPDATE WHERE version=expected, 1 jitter retry). If
        both attempts collide with another writer the
        :class:`ConcurrencyError` propagates so the API router can
        emit a ``409`` with ``code=session_version_mismatch``.

        Raises:
          * :class:`SessionNotFound` — unknown id
          * :class:`SessionNotRunning` — not currently RUNNING
          * :class:`MaxPausedExceeded` — would exceed global or per-key cap
          * :class:`BridgeAckTimeout` — runner didn't ack in time
          * :class:`ConcurrencyError` — two concurrent pause calls
            both failed the version check after one retry
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
        # Read the row's optimistic-locking anchor BEFORE the bridge ack
        # so the version-checked write below is anchored to a recent
        # value. Doing it after the ack would race with internal
        # ``_run`` writes that bump the row mid-flight.
        row = await self._store.get_session(sid)
        if row is None:
            raise SessionNotFound(sid)
        expected_v = int(row["version"])
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
        await self._update_status_locked(
            sid,
            expected_version=expected_v,
            status=SessionState.PAUSED.value,
            paused_at=self._paused_at[sid],
        )
        await self._audit_record(
            actor=self._owner_of(sid),
            action="session_pause",
            target_type="session",
            target_id=sid,
            metadata={"reason": reason},
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

        Plan 7 D7.5 / Task 8 — the row transition uses optimistic
        locking via :meth:`_update_status_locked`.

        Raises:
          * :class:`SessionNotFound` — unknown id
          * :class:`SessionNotPaused` — not currently PAUSED
          * :class:`ResumeQueueTimeout` — semaphore couldn't be re-acquired
          * :class:`BridgeAckTimeout` — runner didn't ack
          * :class:`ConcurrencyError` — version-checked write collided
            twice (rare; surfaces as 409 at the API layer)
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
        # Read the row's optimistic-locking anchor before the bridge ack.
        row = await self._store.get_session(sid)
        if row is None:
            raise SessionNotFound(sid)
        expected_v = int(row["version"])
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
        await self._update_status_locked(
            sid,
            expected_version=expected_v,
            status=SessionState.RUNNING.value,
        )
        await self._audit_record(
            actor=self._owner_of(sid),
            action="session_resume",
            target_type="session",
            target_id=sid,
            metadata={"hint": hint},
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

    async def _audit_record(
        self,
        *,
        actor: str,
        action: str,
        target_type: str | None = None,
        target_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Plan 8 D8.4 / Task 5 — best-effort audit write.

        Skips when ``self._audit`` is ``None`` (legacy in-process
        callers without an audit sink). Failures are logged at WARN
        and swallowed: the audit row is observability, NOT a
        precondition for the business mutation. The fallback
        middleware will not double-write — explicit hooks always emit
        a meaningful ``action`` rather than ``unknown_mutation``.
        """
        if self._audit is None:
            return
        try:
            await self._audit.record(
                actor=actor,
                action=action,
                target_type=target_type,
                target_id=target_id,
                metadata=metadata,
            )
        except Exception:
            logger.warning(
                "session %s audit write failed (action=%s, actor=%s)",
                target_id or "?",
                action,
                actor,
                exc_info=True,
            )

    def _owner_of(self, sid: str) -> str:
        """Resolve the actor label for an in-flight session.

        Used by :meth:`cancel` / :meth:`pause` / :meth:`resume` to
        attribute the audit row to the same identity that submitted
        the session in the first place. Falls back to ``"anon"`` so
        the schema's non-NULL constraint is always satisfied.

        Read order:
          1. In-memory ``_owner_by_session`` populated at submit
             (cheap dict lookup; matches the API key label written to
             ``sessions.owner``).
          2. ``"anon"`` when the session was submitted before the
             label-aware owner accounting landed (legacy in-process
             callers, recovered-from-disk sessions).
        """
        owner = self._owner_by_session.get(sid)
        if owner:
            return str(owner)
        return "anon"

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
            # Plan 7 D7.5: optimistic-lock internal transitions too so
            # the version counter is monotonic across pause/_run races.
            # ConcurrencyError after 1 retry means an external writer
            # already moved the row — log and continue (the external
            # state is authoritative).
            try:
                await self._update_status_locked(
                    sid,
                    status=SessionState.RUNNING.value,
                    started_at=_utcnow(),
                )
            except ConcurrencyError as exc:
                logger.info(
                    "session %s queued→running version race; external "
                    "state wins (expected=%s actual=%s)",
                    sid,
                    exc.expected_version,
                    exc.actual_version,
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
                try:
                    await self._update_status_locked(
                        sid, runtime_id=handle.runtime_id
                    )
                except ConcurrencyError as exc:
                    logger.info(
                        "session %s runtime_id write race; expected=%s "
                        "actual=%s",
                        sid,
                        exc.expected_version,
                        exc.actual_version,
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
            # Plan 7 D7.25 / Task 14 — classify raw SDK exceptions
            # into the typed taxonomy so the API + dashboard can
            # surface ``error_category`` instead of bare class names.
            # Non-SDK exceptions (e.g. internal asserts) pass through
            # ``classify_sdk_error`` and bucket as ``unknown`` which is
            # the correct fallback for unrecognised failure modes.
            sdk_err = classify_sdk_error(exc)
            end_status = "failed"
            end_reason = f"{sdk_err.category}:{sdk_err.http_status}"
            logger.exception(
                "session %s failed category=%s http_status=%s",
                sid,
                sdk_err.category,
                sdk_err.http_status,
            )
            await self._bus.publish(
                InstallError(
                    session_id=sid,
                    code=sdk_err.category,
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
            self._owner_by_session.pop(sid, None)
            # Plan 7 D7.5: terminal-state write is optimistically
            # locked but ConcurrencyError is suppressed — if cancel /
            # pause raced into a terminal state already, that outcome
            # wins over our default ``end_status``.
            try:
                await self._update_status_locked(
                    sid,
                    status=end_status,
                    ended_at=_utcnow(),
                    end_reason=end_reason,
                )
            except ConcurrencyError as exc:
                logger.info(
                    "session %s terminal write race; external state "
                    "wins (expected=%s actual=%s)",
                    sid,
                    exc.expected_version,
                    exc.actual_version,
                )
            except SessionNotFound:
                # Row deleted out from under us (rare; defensive).
                pass
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

    async def _update_status_locked(
        self,
        sid: str,
        *,
        expected_version: int | None = None,
        **fields: Any,
    ) -> int:
        """Version-checked ``update_session_status`` with 1 jitter retry.

        Plan 7 D7.5 / Task 8. Reads the current version (or accepts
        the caller's ``expected_version`` anchor — used by pause()
        which read the version before sending the bridge ack so the
        write happens against a known-recent value).

        Retries **once** with a small jitter on
        :class:`ConcurrencyError` so the common "rapid pause →
        internal state-write race" resolves transparently. After the
        second failure the caller decides what to do: pause/resume
        re-raise (the API router maps to 409), the manager's
        internal ``_run`` finally block logs + suppresses so a
        crash-on-shutdown race never breaks the lifecycle bookkeeping.
        """
        if expected_version is None:
            row = await self._store.get_session(sid)
            if row is None:
                raise SessionNotFound(sid)
            expected_version = int(row["version"])
        last_exc: ConcurrencyError | None = None
        for attempt in (1, 2):
            try:
                return await self._store.update_session_status(
                    sid, expected_version=expected_version, **fields
                )
            except ConcurrencyError as exc:
                last_exc = exc
                if attempt == 2:
                    raise
                await asyncio.sleep(random.random() * _RETRY_JITTER_MAX_S)
                if exc.actual_version is not None:
                    expected_version = int(exc.actual_version)
                else:
                    row = await self._store.get_session(sid)
                    if row is None:
                        raise SessionNotFound(sid) from exc
                    expected_version = int(row["version"])
        # Defensive: the loop always returns or raises.
        assert last_exc is not None
        raise last_exc

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
        #
        # Plan 7 D7.19 / Task 14 — ``runtime_ctx`` is also threaded
        # through as a kwarg so the in-process runner factory can
        # inject ``RELAY_TRACE_ID`` into the SDK env. Production
        # ``_build_executor_factory`` accepts it; legacy 5-arg
        # (control_channel only) factories trigger ``TypeError`` and
        # we fall through to the older signatures.
        channel = ControlChannel()
        try:
            executor = self._executor_factory(
                spec.executor,
                policy,
                self._coordinator,
                sid,
                control_channel=channel,
                runtime_ctx=runtime_ctx,
            )
        except TypeError:
            try:
                executor = self._executor_factory(
                    spec.executor,
                    policy,
                    self._coordinator,
                    sid,
                    control_channel=channel,
                )
            except TypeError:
                # Backwards-compat for legacy 4-arg factories that
                # don't accept either kwarg yet — pause/resume +
                # trace_id injection are unavailable but everything
                # else continues to work.
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

    def _arm_paused_timer(
        self, sid: str, *, remaining_s: float | None = None
    ) -> None:
        """Arm (or re-arm) the paused-timeout watchdog for ``sid``.

        Plan 7 D7.18 / Task 14 — ``remaining_s`` is used by
        :func:`gg_relay.session.recovery.recover_paused_timers` when
        the relay restarts mid-pause: the recovery hook computes the
        elapsed-since-paused time and re-arms the watchdog with the
        original deadline (``paused_timeout_s - elapsed``) instead of
        a fresh full window.

        Idempotent — any pre-existing timer for ``sid`` is cancelled
        before the new one is created so repeat calls never leak
        timers.
        """
        self._cancel_paused_timer(sid)
        sleep_s = (
            float(remaining_s)
            if remaining_s is not None
            else float(self._paused_timeout_s)
        )
        self._paused_timers[sid] = asyncio.create_task(
            self._paused_timeout_watchdog(sid, sleep_s),
            name=f"paused-timeout-{sid}",
        )

    def _cancel_paused_timer(self, sid: str) -> None:
        timer = self._paused_timers.pop(sid, None)
        if timer is not None and not timer.done():
            timer.cancel()

    async def _paused_timeout_watchdog(
        self, sid: str, sleep_s: float | None = None
    ) -> None:
        wait_s = sleep_s if sleep_s is not None else float(self._paused_timeout_s)
        try:
            await asyncio.sleep(wait_s)
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
        """Capture the ``session.end`` frame's token / cost / turn aggregates.

        Plan 6 D6.12 — the actual DB write happens in :meth:`_run`'s
        finally so it can be batched into the terminal-state write.

        Plan 7 D7.19 / Task 14 — field-name resolution mirrors
        :class:`gg_relay.tracing.metrics_subscriber.MetricsSubscriber`:
        canonical names (``input_tokens`` / ``output_tokens``) at the
        frame's top level win over the legacy nested
        ``tokens={"input_tokens", "output_tokens"}`` shape and over
        the even-older ``tokens={"in", "out"}`` shape. Cost reads
        ``cost_usd`` at the top level. Any field that resolves to a
        falsy value falls through to the next candidate so partial
        frames don't poison aggregates with zeros.
        """
        metrics = self._metrics.get(sid)
        if metrics is None:
            return
        tokens = frame.get("tokens") or {}
        if not isinstance(tokens, dict):
            tokens = {}
        in_toks = (
            frame.get("input_tokens")
            or tokens.get("input_tokens")
            or tokens.get("in")
            or 0
        )
        out_toks = (
            frame.get("output_tokens")
            or tokens.get("output_tokens")
            or tokens.get("out")
            or 0
        )
        turn_count = (
            frame.get("turn_count") or tokens.get("turn_count") or 0
        )
        try:
            metrics.input_tokens = int(in_toks or 0)
            metrics.output_tokens = int(out_toks or 0)
            metrics.turn_count = int(turn_count or 0)
        except (TypeError, ValueError):
            logger.debug("non-int token values in session.end frame sid=%s", sid)
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
