"""GgRelayClaudeClient + make_sdk_runner / make_wire_runner.

The runner factory wires together:
  - ClaudeSDKClient (or stub) — owns the actual SDK conversation
  - ClaudeCodeOptions.can_use_tool — host-side ToolPolicy + HITLCoordinator
  - SessionTransport — pipes SDK events as event frames to the *consumer*
    (host in-process, host bridge over unix-socket, or future K8s service)

Plan 2 changes vs Plan 1:
  - dispatch on real SDK dataclasses (UserMessage / AssistantMessage / SystemMessage /
    ResultMessage / StreamEvent) via ``match`` — no more dict-stub path
  - HITL req_id ↔ SDK tool_use_id pairing via bidirectional FIFO over
    ``(tool_name, canonical(input))`` (see docs/sdk-message-ordering-spike.md)

Plan 3 changes vs Plan 2:
  - The dispatch body is extracted into :func:`_make_runner_core` and consumed
    by two thin factories:
      * :func:`make_sdk_runner`  — in-process; ``coordinator`` is a real
                                   :class:`HITLCoordinator` living in the host loop.
      * :func:`make_wire_runner` — in-container; ``coordinator`` is a
                                   :class:`WireCoordinatorProxy` that ferries
                                   decisions across the unix-socket transport.
    Both factories use a duck-typed ``coordinator.request(req_id, tool=..., args=...)``
    surface; the host vs. wire distinction is invisible to the dispatch loop.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import traceback
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, is_dataclass
from typing import Any, Protocol

from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_code_sdk.types import StreamEvent

from gg_relay.core.exceptions import SDKPermissionError
from gg_relay.session.control import (
    AckSender,
    ControlChannel,
    ControlLoop,
    cancel_control_task,
)
from gg_relay.session.frames import (
    make_error,
    make_install_done,
    make_msg_chunk,
    make_session_end,
    make_tool_request,
    make_tool_result,
)
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.plugins import InstallReport
from gg_relay.session.runner.proxy_client import WireCoordinatorProxy
from gg_relay.session.spec import Decision, SessionRuntimeContext, SessionSpec
from gg_relay.session.transport.protocol import SessionTransport

SdkFactory = Callable[[ClaudeCodeOptions], Any]
"""Factory returning a ClaudeSDKClient-like object.

Returns Any (not ClaudeSDKClient) so test stubs can satisfy the duck-typed
surface (connect/disconnect/query/receive_messages) without inheriting from
the real SDK class.
"""


class _CoordinatorLike(Protocol):
    """Duck-type both HITLCoordinator and WireCoordinatorProxy satisfy.

    The dispatch loop never inspects the coordinator beyond this signature —
    keeps in-process and wire-mode runners interchangeable.

    ``session_id`` is optional so the wire proxy (which simply forwards the
    decision across the transport) can ignore it; the in-process
    HITLCoordinator uses it for cancel_all scoping.
    """

    async def request(
        self,
        req_id: str,
        *,
        tool: str,
        args: dict[str, Any],
        session_id: str = ...,
    ) -> Any: ...


RunnerCallable = Callable[[SessionTransport, SessionSpec], Awaitable[None]]
"""Generalised RunnerFn that accepts any SessionTransport. The
executor/protocol RunnerFn alias is still used by InProcessExecutor (which
hands the runner an InMemoryTransport specifically); make_wire_runner returns
this broader callable shape because UnixSocketTransport also satisfies the
SessionTransport Protocol."""


# ── input canonicalization for FIFO matching ───────────────────────────────


_FrozenInput = frozenset[tuple[str, str]]


def _freeze(d: dict[str, Any]) -> _FrozenInput:
    """Canonical, hashable representation of a tool-call input.

    Nested mutables (dict / list / tuple) are flattened via
    ``json.dumps(sort_keys=True)`` so ``{"a": 1, "b": [2, 3]}`` and
    ``{"b": [2, 3], "a": 1}`` hash identically. All tool inputs are JSON-shaped
    coming from the CLI, so this is round-trip stable.
    """
    return frozenset(
        (k, json.dumps(v, sort_keys=True, default=str)) for k, v in d.items()
    )


# ── content-block serialization (for msg.chunk payloads) ───────────────────


def _serialize_block(block: Any) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "signature": block.signature}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "is_error": block.is_error,
        }
    if is_dataclass(block) and not isinstance(block, type):
        return {"type": type(block).__name__, **asdict(block)}
    return {"type": type(block).__name__, "repr": repr(block)}


def _serialize_assistant(m: AssistantMessage) -> dict[str, Any]:
    return {
        "type": "AssistantMessage",
        "model": m.model,
        "parent_tool_use_id": m.parent_tool_use_id,
        "content": [_serialize_block(b) for b in m.content],
    }


def _serialize_user(m: UserMessage) -> dict[str, Any]:
    if isinstance(m.content, str):
        content: str | list[dict[str, Any]] = m.content
    else:
        content = [_serialize_block(b) for b in m.content]
    return {
        "type": "UserMessage",
        "parent_tool_use_id": m.parent_tool_use_id,
        "content": content,
    }


def _serialize_misc(m: Any) -> dict[str, Any]:
    if isinstance(m, SystemMessage):
        return {"type": "SystemMessage", "subtype": m.subtype, "data": m.data}
    if isinstance(m, StreamEvent):
        return {
            "type": "StreamEvent",
            "uuid": m.uuid,
            "session_id": m.session_id,
            "event": m.event,
            "parent_tool_use_id": m.parent_tool_use_id,
        }
    if is_dataclass(m) and not isinstance(m, type):
        return {"type": type(m).__name__, **asdict(m)}
    return {"type": type(m).__name__, "repr": repr(m)}


def _serialize_tool_result(block: ToolResultBlock) -> dict[str, Any]:
    """Render a ToolResultBlock as the ``result`` payload of a tool.result frame."""
    return {"content": block.content, "is_error": block.is_error}


# ── pre-run argv execution ────────────────────────────────────────────────
#
# Per-command and total wall-clock budgets for spec.plugins.pre_run_cmds.
# Output is streamed as msg.chunk frames; total bytes are capped to keep
# misbehaving commands from flooding the transport / store. terminate()
# is followed by a kill() fallback so a process ignoring SIGTERM cannot
# stall cancellation (Reviewer Round 2 finding).
PRE_RUN_PER_CMD_TIMEOUT_S = 60.0
PRE_RUN_TOTAL_TIMEOUT_S = 300.0
PRE_RUN_OUTPUT_LIMIT_BYTES = 64 * 1024
PRE_RUN_KILL_GRACE_S = 5.0


# ── upstream auth-failure guards (relay-side safety nets) ──────────────
#
# The Claude CLI binary emits a synthetic AssistantMessage with
# ``model="<synthetic>"`` followed by a ResultMessage when its internal
# ``max_retries=10`` is exhausted (e.g. ANTHROPIC_API_KEY rejected by
# the upstream API). Without the two guards below the runner would
# treat that as a normal ``completed`` session with zero tokens — a
# misleading dashboard row that hides credential misconfiguration.
#
# ``SYNTHETIC_MODEL_MARKER`` is the model string the CLI uses for the
# fabricated terminal message. Pinned as a module constant so the
# detection logic is one-string-replace away from future SDK rewrites.
SYNTHETIC_MODEL_MARKER = "<synthetic>"

# Subtype carried by the SDK's ``SystemMessage`` frames when the
# upstream API rejects a request and the CLI is retrying.
_API_RETRY_SUBTYPE = "api_retry"


def _extract_synthetic_text(msg: AssistantMessage) -> str:
    """Concatenate ``TextBlock`` content from a synthetic AssistantMessage.

    The CLI's fabricated terminal message always carries a single
    TextBlock like ``"Failed to authenticate. API Error: 401 ..."``.
    We grab every TextBlock to be defensive against multi-block
    variants the SDK may introduce, and silently ignore non-text
    blocks (tool_use / thinking) since they don't carry the
    actionable error text.
    """
    parts: list[str] = []
    for block in msg.content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return " ".join(parts) if parts else "synthetic AssistantMessage"


def _format_retry_budget_error(
    count: int,
    budget: int,
    last_payload: dict[str, Any] | None,
) -> str:
    """Build a diagnostic message for ``SDKPermissionError`` raised by
    the ``api_retry`` budget guard.

    Includes ``error_status`` + ``error`` from the last seen retry
    payload when present so the operator sees the upstream HTTP code
    (typically 401 / 403) in the dashboard's ``end_reason`` column
    without having to dig into the raw frame stream.
    """
    base = (
        f"upstream api_retry budget exhausted "
        f"(count={count} budget={budget})"
    )
    if not isinstance(last_payload, dict):
        return base
    status = last_payload.get("error_status")
    err = last_payload.get("error")
    if status is None and err is None:
        return base
    return f"{base}: error_status={status} error={err!r}"


async def _terminate_proc(proc: asyncio.subprocess.Process) -> None:
    """Cooperative SIGTERM → SIGKILL fallback; safe for already-exited procs."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=PRE_RUN_KILL_GRACE_S)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()


async def _execute_pre_run_cmds(
    transport: SessionTransport,
    cmds: tuple[tuple[str, ...], ...],
    start_seq: int,
    session_id: str,
) -> int:
    """Run pre-run argv commands sequentially before the SDK starts.

    Each command is invoked via :func:`asyncio.create_subprocess_exec`
    (no shell interpretation). Stdout/err are streamed as ``msg.chunk``
    frames with ``stream="pre_run"`` so the dashboard can render them
    inline with the session timeline. Total output bytes per session
    are capped at :data:`PRE_RUN_OUTPUT_LIMIT_BYTES` to bound store
    growth; once the cap is hit further chunks are dropped.

    Failure modes (each emits an ``error`` frame, then raises so the
    enclosing executor sees a non-zero session outcome):
      * ``FileNotFoundError`` / ``PermissionError`` / ``OSError`` —
        process did not start (executable missing, no exec bit, …).
      * non-zero exit code.
      * per-command timeout (:data:`PRE_RUN_PER_CMD_TIMEOUT_S`).
      * total timeout (:data:`PRE_RUN_TOTAL_TIMEOUT_S`).

    Returns the next ``seq`` value the caller should use for follow-on
    frames so the runner-wide sequence stays monotonic.
    """
    seq = start_seq
    bytes_emitted = 0

    async def _stream_proc_output(
        proc: asyncio.subprocess.Process,
    ) -> bytearray:
        nonlocal seq, bytes_emitted
        captured = bytearray()
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            # capture for traceback even after we stop forwarding
            if len(captured) < PRE_RUN_OUTPUT_LIMIT_BYTES:
                captured.extend(
                    chunk[: PRE_RUN_OUTPUT_LIMIT_BYTES - len(captured)]
                )
            if bytes_emitted < PRE_RUN_OUTPUT_LIMIT_BYTES:
                remaining = PRE_RUN_OUTPUT_LIMIT_BYTES - bytes_emitted
                forwarded = chunk[:remaining]
                bytes_emitted += len(forwarded)
                await transport.send(
                    make_msg_chunk(
                        seq,
                        {
                            "text": forwarded.decode(
                                "utf-8", errors="replace"
                            ),
                            "stream": "pre_run",
                            "session_id": session_id,
                        },
                    )
                )
                seq += 1
                if bytes_emitted >= PRE_RUN_OUTPUT_LIMIT_BYTES:
                    await transport.send(
                        make_msg_chunk(
                            seq,
                            {
                                "text": (
                                    f"[pre_run] output truncated at "
                                    f"{PRE_RUN_OUTPUT_LIMIT_BYTES} bytes\n"
                                ),
                                "stream": "pre_run",
                                "session_id": session_id,
                            },
                        )
                    )
                    seq += 1
        return captured

    async def _emit_error(code: str, message: str, tb: str | None) -> None:
        nonlocal seq
        await transport.send(make_error(seq, code, message, traceback_=tb))
        seq += 1

    async def _run_one(argv: tuple[str, ...]) -> None:
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            await _emit_error(
                "pre_run_spawn_failed",
                f"failed to spawn argv={list(argv)}: {exc}",
                traceback.format_exc(),
            )
            raise RuntimeError(
                f"pre_run_cmd failed to spawn: {list(argv)}"
            ) from exc

        try:
            captured = await _stream_proc_output(proc)
            rc = await proc.wait()
        except asyncio.CancelledError:
            await _terminate_proc(proc)
            raise
        if rc != 0:
            tail = captured.decode("utf-8", errors="replace")
            await _emit_error(
                "pre_run_failed",
                f"pre_run argv={list(argv)} exit={rc}",
                tail,
            )
            raise RuntimeError(
                f"pre_run_cmd non-zero exit: argv={list(argv)} rc={rc}"
            )

    try:
        async with asyncio.timeout(PRE_RUN_TOTAL_TIMEOUT_S):
            for argv in cmds:
                async with asyncio.timeout(PRE_RUN_PER_CMD_TIMEOUT_S):
                    await _run_one(argv)
    except TimeoutError as exc:
        await _emit_error(
            "pre_run_timeout",
            "pre_run_cmds exceeded time budget",
            None,
        )
        raise RuntimeError("pre_run_cmds timed out") from exc

    return seq


# ── runner factory ─────────────────────────────────────────────────────────


async def _make_runner_core(
    transport: SessionTransport,
    spec: SessionSpec,
    *,
    coordinator: _CoordinatorLike,
    policy: ToolPolicy,
    sdk_factory: SdkFactory,
    install_report: InstallReport | None,
    session_id: str = "",
    control_channel: ControlChannel | None = None,
    control_ack: AckSender | None = None,
    runtime_ctx: SessionRuntimeContext | None = None,
    api_retry_budget: int = 0,
) -> None:
    """Shared dispatch loop for the in-process and wire runners.

    Owns one SDK conversation: connect → query → drain receive_messages →
    disconnect (in finally). Each SDK message is serialised onto ``transport``
    as the appropriate EventFrame; ``can_use_tool`` consults ``policy`` and,
    on ``NEEDS_HITL``, awaits ``coordinator.request(...)`` (which is either
    a real :class:`HITLCoordinator` or a :class:`WireCoordinatorProxy`).

    HITL mapping (bidirectional FIFO, see ``docs/sdk-message-ordering-spike.md``):
      Two deques are maintained per-runner so either event order is handled:

      * ``pending_perms``      — ``(req_id, name, frozen_input)`` queued by
                                 ``can_use_tool`` when no matching
                                 :class:`ToolUseBlock` has been seen yet.
      * ``pending_use_blocks`` — ``(tool_use_id, name, frozen_input)`` queued
                                 by ``AssistantMessage`` processing when no
                                 matching pending perm exists yet.

      Whichever side arrives second pops its match from the opposing queue
      and registers the ``tool_use_id → req_id`` mapping. The mapping is
      consulted when a ``UserMessage(ToolResultBlock)`` arrives.
    """
    seq = 0

    # Install report (if any) is the very first frame so the host can render
    # "plugins installed: X" before the SDK starts streaming. SessionManager
    # / handler runs assembler.prepare() before executor.start(); the
    # resulting InstallReport is threaded in here. The wire-mode runner
    # always passes None — the install is baked into the docker image.
    if install_report is not None:
        await transport.send(make_install_done(seq, install_report))
        seq += 1

    # Pre-run argv list (e.g. git fetch / git worktree add) — executed before
    # the SDK starts so each session can dynamically prepare its working tree
    # in the runner container. Schema layer (SessionSpecIn) restricts this to
    # docker executor; argv-only inputs prevent shell injection. Failures
    # raise to abort the session before the SDK is contacted.
    if spec.plugins.pre_run_cmds:
        seq = await _execute_pre_run_cmds(
            transport, spec.plugins.pre_run_cmds, seq, session_id
        )

    pending_perms: deque[tuple[str, str, _FrozenInput]] = deque()
    pending_use_blocks: deque[tuple[str, str, _FrozenInput]] = deque()
    use_id_to_req_id: dict[str, str] = {}

    def _pair_perm_with_block(name: str, fi: _FrozenInput) -> str | None:
        for idx, (uid, n, f) in enumerate(pending_use_blocks):
            if n == name and f == fi:
                del pending_use_blocks[idx]
                return uid
        return None

    def _pair_block_with_perm(name: str, fi: _FrozenInput) -> str | None:
        for idx, (rid, n, f) in enumerate(pending_perms):
            if n == name and f == fi:
                del pending_perms[idx]
                return rid
        return None

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        del context  # ToolPermissionContext is unused; SDK requires the param
        d = policy.decide(tool_name, tool_input, spec.cwd)
        if d == Decision.ACCEPT:
            return PermissionResultAllow()
        if d == Decision.DENY:
            return PermissionResultDeny(message=f"policy denied {tool_name}")
        # NEEDS_HITL → publish tool.request, await coordinator decision.
        # 12 hex chars = 48 bits, birthday-bound ~16M concurrent pending
        # (vs 8 hex / 32 bits → only ~65K before 50% collision).
        # Plan 4 D4 namespacing: prefix with session_id when supplied so the
        # coordinator can scope cancel_all / pending_snapshot per session.
        short = uuid.uuid4().hex[:12]
        req_id = f"{session_id}:{short}" if session_id else f"r-{short}"
        fi = _freeze(tool_input)

        matched_uid = _pair_perm_with_block(tool_name, fi)
        if matched_uid is not None:
            use_id_to_req_id[matched_uid] = req_id
        else:
            pending_perms.append((req_id, tool_name, fi))

        nonlocal seq
        seq += 1
        await transport.send(
            make_tool_request(seq, req_id, tool_name, tool_input)
        )
        decision = await coordinator.request(
            req_id, tool=tool_name, args=tool_input, session_id=session_id
        )
        if decision == "accept":
            return PermissionResultAllow()
        return PermissionResultDeny(message="HITL rejected")

    # Plan v3 §A — fold ``runtime_ctx.credentials`` into the SDK env
    # before ``extra_env``. The Plan v3 override order is:
    #   1. runtime_ctx.credentials (per-session secrets — supplied by
    #      API body AND/OR per-user DB rows merged in by the manager)
    #   2. spec.plugins.extra_env  (caller knob; wins over creds, same
    #      precedence as docker ``_build_env``)
    #   3. RELAY_TRACE_ID          (explicit set — system marker that
    #      MUST win over extra_env; inprocess-only convention pinned
    #      by ``test_trace_id_does_not_clobber_existing_env``)
    #   4. CLAUDE_ROOT             (setdefault; extra_env wins)
    #
    # The SDK transport (``claude_code_sdk._internal.transport.subprocess_cli``)
    # merges ``options.env`` on top of ``os.environ``, so any key we
    # do NOT set still inherits from the host — single-tenant
    # deployments relying on a shell-env ``ANTHROPIC_API_KEY`` keep
    # working when ``credentials`` is empty.
    env: dict[str, str] = {}
    if runtime_ctx is not None:
        for k, v in runtime_ctx.credentials.items():
            env[k] = v
    for k, v in spec.plugins.extra_env:
        env[k] = v
    if runtime_ctx is not None and runtime_ctx.trace_id:
        env["RELAY_TRACE_ID"] = runtime_ctx.trace_id
    # Inject CLAUDE_ROOT so the SDK reads skills/commands/rules from the
    # per-session install directory built by InstallShellAssembler. Uses
    # setdefault so spec.plugins.extra_env can still override if needed.
    # Mirrors docker executor behaviour where the runner image ships
    # GG_PLUGINS_HOME; inprocess achieves the same isolation via CLAUDE_ROOT.
    if install_report is not None and install_report.install_root is not None:
        env.setdefault("CLAUDE_ROOT", str(install_report.install_root))
    options = ClaudeCodeOptions(
        can_use_tool=can_use_tool,
        cwd=str(spec.cwd),
        env=env,
    )

    client: Any = None
    control_task: asyncio.Task[None] | None = None
    try:
        client = sdk_factory(options)
        await client.connect()
        await client.query(spec.prompt)
        if control_channel is not None:
            # Plan 6 D6.11: spawn a dedicated control task that owns the
            # SDK handle and drains pause/resume directives. The runner
            # core just hands the task its inputs — the dispatch loop
            # below stays untouched.
            ack: AckSender = control_ack or control_channel.runner_ack
            loop = ControlLoop(
                client=client,
                recv=control_channel.runner_recv,
                ack=ack,
            )
            control_task = asyncio.create_task(
                loop.run(), name=f"runner-control-{session_id or 'inproc'}"
            )
        # Upstream auth-failure guards (relay-side safety nets):
        #   * ``api_retry_count`` — counts ``SystemMessage(subtype="api_retry")``
        #     frames; once it exceeds ``api_retry_budget`` (when > 0) the
        #     runner aborts with :class:`SDKPermissionError` instead of
        #     letting the CLI's internal 10-attempt loop run to a
        #     synthetic ``completed`` finish (~3 minutes of dead air).
        #   * ``last_synthetic_text`` — non-None whenever the most-recent
        #     AssistantMessage carried ``model == SYNTHETIC_MODEL_MARKER``.
        #     Catches the case where the CLI exhausted its OWN retries
        #     before our budget kicked in (e.g. budget=0). On the next
        #     ResultMessage the runner re-classifies that "completed"
        #     into a permission failure so the dashboard surfaces the
        #     real cause instead of a misleading zero-token success.
        api_retry_count = 0
        last_api_retry_payload: dict[str, Any] | None = None
        last_synthetic_text: str | None = None
        async for msg in client.receive_messages():
            seq += 1
            match msg:
                case ResultMessage():
                    if last_synthetic_text is not None:
                        # Runtime guard B — synthetic AssistantMessage
                        # immediately before a ResultMessage means the
                        # CLI gave up internally and is reporting a
                        # fake "completion". Raise so manager classifies
                        # this as a failure instead of writing
                        # status=completed with zero tokens.
                        raise SDKPermissionError(
                            "upstream auth failure: synthetic completion "
                            f"({last_synthetic_text[:200]})"
                        )
                    seq += 1
                    await transport.send(
                        make_session_end(
                            seq,
                            "completed",
                            tokens=(
                                dict(msg.usage) if msg.usage is not None else {}
                            ),
                            cost_usd=msg.total_cost_usd or 0.0,
                        )
                    )
                    break
                case AssistantMessage():
                    # Track synthetic-model flag for the ResultMessage
                    # guard above. Real AssistantMessages clear it so a
                    # legitimate finish after recovery still completes
                    # cleanly.
                    if getattr(msg, "model", None) == SYNTHETIC_MODEL_MARKER:
                        last_synthetic_text = _extract_synthetic_text(msg)
                    else:
                        last_synthetic_text = None
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            fi = _freeze(block.input)
                            matched_rid = _pair_block_with_perm(block.name, fi)
                            if matched_rid is not None:
                                use_id_to_req_id[block.id] = matched_rid
                            else:
                                pending_use_blocks.append(
                                    (block.id, block.name, fi)
                                )
                    await transport.send(
                        make_msg_chunk(seq, _serialize_assistant(msg))
                    )
                case UserMessage():
                    tool_results = (
                        [b for b in msg.content if isinstance(b, ToolResultBlock)]
                        if isinstance(msg.content, list)
                        else []
                    )
                    if tool_results:
                        for block in tool_results:
                            req_id = use_id_to_req_id.pop(block.tool_use_id, "")
                            seq += 1
                            await transport.send(
                                make_tool_result(
                                    seq,
                                    req_id=req_id,
                                    ok=not bool(block.is_error),
                                    result=_serialize_tool_result(block),
                                )
                            )
                    else:
                        await transport.send(
                            make_msg_chunk(seq, _serialize_user(msg))
                        )
                case SystemMessage() | StreamEvent():
                    # Runtime guard A — count upstream ``api_retry``
                    # frames and bail out once the relay-side budget
                    # is exceeded. ``budget == 0`` disables the guard
                    # so guard B (synthetic-completion detection) is
                    # the only safety net.
                    if (
                        isinstance(msg, SystemMessage)
                        and getattr(msg, "subtype", None) == _API_RETRY_SUBTYPE
                    ):
                        api_retry_count += 1
                        if isinstance(msg.data, dict):
                            last_api_retry_payload = msg.data
                        if (
                            api_retry_budget > 0
                            and api_retry_count > api_retry_budget
                        ):
                            await transport.send(
                                make_msg_chunk(seq, _serialize_misc(msg))
                            )
                            err_msg = _format_retry_budget_error(
                                api_retry_count,
                                api_retry_budget,
                                last_api_retry_payload,
                            )
                            raise SDKPermissionError(err_msg)
                    await transport.send(
                        make_msg_chunk(seq, _serialize_misc(msg))
                    )
                case _:
                    await transport.send(
                        make_msg_chunk(seq, _serialize_misc(msg))
                    )
    except asyncio.CancelledError:
        # Clean cancellation (e.g. executor.stop()) — propagate without
        # publishing a misleading `error` frame. The transport-close path
        # tells the host the session ended.
        raise
    except BaseException as exc:
        # Per RunnerFn contract: runner exceptions must surface to the host
        # as an `error` frame before propagating, otherwise the host only
        # sees TransportClosed and loses the root cause. Catch BaseException
        # so KeyboardInterrupt / SystemExit also publish the frame before
        # unwinding. Suppress any send failure so the original exception is
        # re-raised intact.
        seq += 1
        with contextlib.suppress(Exception):
            await transport.send(
                make_error(
                    seq,
                    type(exc).__name__,
                    str(exc),
                    traceback_=traceback.format_exc(),
                )
            )
        raise
    finally:
        await cancel_control_task(control_task)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()


def make_sdk_runner(
    *,
    policy: ToolPolicy,
    coordinator: HITLCoordinator,
    sdk_factory: SdkFactory = ClaudeSDKClient,
    install_report: InstallReport | None = None,
    session_id: str = "",
    control_channel: ControlChannel | None = None,
    runtime_ctx: SessionRuntimeContext | None = None,
    api_retry_budget: int = 0,
) -> RunnerCallable:
    """In-process runner factory.

    ``coordinator`` is a real :class:`HITLCoordinator` living in the host
    event loop; HITL decisions are awaited directly without round-tripping
    through the transport.

    ``session_id``, when supplied, is used to namespace generated req_ids
    so that :meth:`HITLCoordinator.cancel_all(session_id=...)` can scope a
    bulk cancel to a single session.

    Plan 6 D6.11: an optional ``control_channel`` enables the same
    pause/resume control-loop the wire runner uses. SessionManager
    builds the channel and stashes it on the inprocess bridge so it can
    push pause/resume directly without crossing a transport.

    Plan 7 D7.19 / Task 14: optional ``runtime_ctx`` is consulted by
    the runner core to inject ``RELAY_TRACE_ID`` into
    :class:`ClaudeCodeOptions.env`, mirroring the docker executor's
    env composition. Tests that don't need trace correlation can omit
    it.
    """

    async def runner(transport: SessionTransport, spec: SessionSpec) -> None:
        await _make_runner_core(
            transport,
            spec,
            coordinator=coordinator,
            policy=policy,
            sdk_factory=sdk_factory,
            install_report=install_report,
            session_id=session_id,
            control_channel=control_channel,
            control_ack=(control_channel.runner_ack if control_channel else None),
            runtime_ctx=runtime_ctx,
            api_retry_budget=api_retry_budget,
        )

    return runner


def make_wire_runner(
    *,
    policy: ToolPolicy,
    coordinator: WireCoordinatorProxy,
    sdk_factory: SdkFactory = ClaudeSDKClient,
    session_id: str = "",
    api_retry_budget: int = 0,
) -> RunnerCallable:
    """Container-side runner factory.

    ``coordinator`` is a :class:`WireCoordinatorProxy` whose
    ``consume_loop()`` MUST already be running on a sibling task — the proxy
    bridges ``tool.request`` (sent by this runner) to ``tool.decision``
    (received from the host). ``install_report`` is intentionally not
    accepted: the install happened at image build-time so the host emits the
    ``install.done`` frame separately (Plan 3 §6 Task 4).

    Plan 6 D6.11: the proxy also owns a :class:`ControlChannel`; the
    runner core spawns a :class:`ControlLoop` that drains pause/resume
    directives and ships acks back as frames via the proxy.
    """

    async def runner(transport: SessionTransport, spec: SessionSpec) -> None:
        await _make_runner_core(
            transport,
            spec,
            coordinator=coordinator,
            policy=policy,
            sdk_factory=sdk_factory,
            install_report=None,
            session_id=session_id,
            control_channel=coordinator.control_channel,
            control_ack=coordinator.send_ack,
            api_retry_budget=api_retry_budget,
        )

    return runner
