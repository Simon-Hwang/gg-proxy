"""GgRelayClaudeClient + make_sdk_runner.

The runner factory wires together:
  - ClaudeSDKClient (or stub) — owns the actual SDK conversation
  - ClaudeCodeOptions.can_use_tool — host-side ToolPolicy + HITLCoordinator
  - InMemoryTransport (runner side) — pipes SDK events as event frames to the host

The host side of the transport is consumed by the calling handler / SessionManager
(out of scope for this Plan; will be added in Plan 4).
"""
from __future__ import annotations

import asyncio
import contextlib
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from claude_code_sdk import (
    ClaudeCodeOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from gg_relay.session.executor.protocol import RunnerFn
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.spec import Decision, SessionSpec
from gg_relay.session.transport.inmemory import InMemoryTransport
from gg_relay.session.transport.protocol import EventFrame

SdkFactory = Callable[[ClaudeCodeOptions], Any]
"""Factory returning a ClaudeSDKClient-like object.

Returns Any (not ClaudeSDKClient) so test stubs can satisfy the duck-typed
surface (connect/disconnect/query/receive_messages) without inheriting from
the real SDK class.
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _envelope(seq: int, type_: str, **rest: Any) -> dict[str, Any]:
    """Build a wire-format frame dict for transport.send.

    Sequence numbering note: `seq` is **monotonic but not gapless**. The
    ResultMessage branch in the runner increments `seq` twice — once for the
    assistant message body itself, once for the trailing `session.end` frame —
    so consumers must not assume contiguous integer ranges. They should only
    rely on strict ordering (later frame ⇒ strictly larger `seq`).
    """
    return {"v": 1, "type": type_, "seq": seq, "ts": _now_iso(), **rest}


def make_sdk_runner(
    *,
    policy: ToolPolicy,
    coordinator: HITLCoordinator,
    sdk_factory: SdkFactory = ClaudeSDKClient,
) -> RunnerFn:
    """Return a RunnerFn suitable for InProcessExecutor(runner=...).

    **SCOPE — Plan 1, in-process only.** This factory takes the host's
    HITLCoordinator directly; it never consumes ControlFrames from the
    transport. For cross-process backends (Plan 3 Docker, future K8s),
    a separate ``make_wire_runner`` will route HITL decisions via
    ``tool.decision`` ControlFrames instead.

    The returned coroutine owns the lifecycle of one SDK conversation:
    connect → query → drain receive_messages → disconnect (in finally).
    Each SDK message becomes a transport EventFrame on the runner side, which
    propagates to the host via the paired InMemoryTransport.
    """

    async def runner(transport: InMemoryTransport, spec: SessionSpec) -> None:
        seq = 0

        async def can_use_tool(
            tool_name: str,
            tool_input: dict[str, Any],
            context: ToolPermissionContext,
        ) -> PermissionResultAllow | PermissionResultDeny:
            d = policy.decide(tool_name, tool_input, spec.cwd)
            if d == Decision.ACCEPT:
                return PermissionResultAllow()
            if d == Decision.DENY:
                return PermissionResultDeny(message=f"policy denied {tool_name}")
            # NEEDS_HITL → publish tool.request, await coordinator decision.
            # 12 hex chars = 48 bits, birthday-bound ~16M concurrent pending
            # (vs 8 hex / 32 bits → only ~65K before 50% collision).
            req_id = f"r-{uuid.uuid4().hex[:12]}"
            nonlocal seq
            seq += 1
            await transport.send(cast(EventFrame, _envelope(
                seq, "tool.request", req_id=req_id, tool=tool_name, args=tool_input,
            )))
            decision = await coordinator.request(req_id, tool=tool_name, args=tool_input)
            if decision == "accept":
                return PermissionResultAllow()
            return PermissionResultDeny(message="HITL rejected")

        options = ClaudeCodeOptions(
            can_use_tool=can_use_tool,
            cwd=str(spec.cwd),
            env=dict(spec.plugins.extra_env),
        )

        # sdk_factory() must be invoked INSIDE the try so a factory that raises
        # synchronously (bad options, ImportError, etc.) still surfaces as an
        # `error` event frame per RunnerFn contract (I-1). `client` is bound
        # to None first so the finally clause can guard against the
        # never-constructed case.
        client: Any = None
        try:
            client = sdk_factory(options)
            await client.connect()
            await client.query(spec.prompt)
            async for msg in client.receive_messages():
                seq += 1
                msg_type = msg.get("type") if isinstance(msg, dict) else type(msg).__name__
                if msg_type == "ToolResult" and isinstance(msg, dict):
                    await transport.send(cast(EventFrame, _envelope(
                        seq, "tool.result",
                        # TODO Plan 4: map SDK tool_use_id → host-side req_id; for now
                        # the dict-stub path passes req_id through verbatim or "".
                        req_id=msg.get("req_id", ""),
                        # Fail-safe default: a ToolResult without an "ok" field means
                        # we don't know if it succeeded, so treat it as failure.
                        ok=msg.get("ok", False),
                        result=msg.get("result", {}),
                    )))
                elif msg_type == "ResultMessage":
                    seq += 1
                    await transport.send(cast(EventFrame, _envelope(
                        seq, "session.end",
                        status="completed",
                        tokens=msg.get("usage", {}) if isinstance(msg, dict) else {},
                        cost_usd=(
                            msg.get("total_cost_usd", 0.0) if isinstance(msg, dict) else 0.0
                        ),
                    )))
                    break
                else:
                    await transport.send(cast(EventFrame, _envelope(
                        seq, "msg.chunk",
                        data=msg if isinstance(msg, dict) else {"repr": repr(msg)},
                    )))
        except asyncio.CancelledError:
            # Clean cancellation (e.g. executor.stop()) — propagate without
            # publishing a misleading `error` frame. runner_wrapper.finally
            # closes the transport so the host observes the session boundary
            # via TransportClosed. (I-2)
            raise
        except BaseException as exc:
            # Per RunnerFn contract (executor/inprocess.py docstring): runner
            # exceptions must surface to the host as an `error` frame before
            # propagating, otherwise the host only sees TransportClosed and
            # loses the root cause. Catch BaseException so KeyboardInterrupt /
            # SystemExit also publish the frame before unwinding. Suppress any
            # send failure so the original exception is re-raised intact.
            seq += 1
            with contextlib.suppress(Exception):
                await transport.send(cast(EventFrame, _envelope(
                    seq, "error",
                    code=type(exc).__name__,
                    message=str(exc),
                    traceback=traceback.format_exc(),
                )))
            raise
        finally:
            # Disconnect must not mask the original exception if it raises,
            # and must be skipped if sdk_factory() never produced a client.
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.disconnect()

    return runner
