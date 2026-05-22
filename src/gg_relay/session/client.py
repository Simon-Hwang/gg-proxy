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
from gg_relay.session.spec import Decision, SessionSpec
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

    options = ClaudeCodeOptions(
        can_use_tool=can_use_tool,
        cwd=str(spec.cwd),
        env=dict(spec.plugins.extra_env),
    )

    client: Any = None
    try:
        client = sdk_factory(options)
        await client.connect()
        await client.query(spec.prompt)
        async for msg in client.receive_messages():
            seq += 1
            match msg:
                case ResultMessage():
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
) -> RunnerCallable:
    """In-process runner factory.

    ``coordinator`` is a real :class:`HITLCoordinator` living in the host
    event loop; HITL decisions are awaited directly without round-tripping
    through the transport.

    ``session_id``, when supplied, is used to namespace generated req_ids
    so that :meth:`HITLCoordinator.cancel_all(session_id=...)` can scope a
    bulk cancel to a single session.
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
        )

    return runner


def make_wire_runner(
    *,
    policy: ToolPolicy,
    coordinator: WireCoordinatorProxy,
    sdk_factory: SdkFactory = ClaudeSDKClient,
    session_id: str = "",
) -> RunnerCallable:
    """Container-side runner factory.

    ``coordinator`` is a :class:`WireCoordinatorProxy` whose
    ``consume_loop()`` MUST already be running on a sibling task — the proxy
    bridges ``tool.request`` (sent by this runner) to ``tool.decision``
    (received from the host). ``install_report`` is intentionally not
    accepted: the install happened at image build-time so the host emits the
    ``install.done`` frame separately (Plan 3 §6 Task 4).
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
        )

    return runner
