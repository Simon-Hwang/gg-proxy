"""SessionTransport Protocol + frame TypedDicts.

帧设计参考 spec §6.2：
  容器 → 宿主 (EventFrame): install.done | msg.chunk | tool.request | tool.result
                            | session.end | error | pong | pause.ack | resume.ack
  宿主 → 容器 (ControlFrame): tool.decision | interrupt | shutdown | ping
                              | pause | resume

Plan 6 D6.11 adds the four control-loop frames (``pause`` / ``resume`` /
``pause.ack`` / ``resume.ack``) so the host can drive ``ClaudeSDKClient.
interrupt()`` and ``query()`` inside the runner via the shared transport.
The ack envelopes carry a host-assigned ``req_id`` so multiple outstanding
host requests don't collide.
"""
from typing import Any, Literal, NotRequired, Protocol, TypedDict, runtime_checkable

# ── Event frames (runner → host) ──────────────────────────────────────────

class _BaseFrame(TypedDict):
    v: int          # protocol version, currently 1
    type: str
    seq: int        # monotonic per-direction
    ts: str         # ISO8601 UTC


class InstallDoneFrame(_BaseFrame):
    """Emitted after PluginAssembler.prepare() succeeds, parsed from
    install-state.json + assembler timing. Plan 2 §6 / Task 4."""

    profile_id: str | None
    modules: list[str]
    duration_ms: int
    install_root: str


class InstallErrorFrame(_BaseFrame):
    """Emitted if PluginAssembler.prepare() fails (post-handler before SDK)."""

    code: str
    message: str
    stderr_tail: NotRequired[str]


class MsgChunkFrame(_BaseFrame):
    data: dict[str, Any]    # SDK message chunk (TextBlock / ToolUseBlock / etc serialized)


class ToolRequestFrame(_BaseFrame):
    req_id: str
    tool: str
    args: dict[str, Any]


class ToolResultFrame(_BaseFrame):
    req_id: str
    ok: bool
    result: NotRequired[dict[str, Any]]
    error: NotRequired[str]


class SessionEndFrame(_BaseFrame):
    status: Literal["completed", "cancelled", "crashed"]
    tokens: NotRequired[dict[str, int]]
    cost_usd: NotRequired[float]


class ErrorFrame(_BaseFrame):
    code: str
    message: str
    traceback: NotRequired[str]


class PongFrame(_BaseFrame):
    pass


class PauseAckFrame(_BaseFrame):
    """Runner → host ack for a host-issued ``pause`` ControlFrame (Plan 6 D6.11).

    ``req_id`` mirrors the ``PauseFrame.req_id`` the host generated so the
    bridge can resolve the matching ``pause()`` future even with multiple
    outstanding pause/resume operations.
    """

    req_id: str
    ok: bool
    error: NotRequired[str]


class ResumeAckFrame(_BaseFrame):
    """Runner → host ack for a host-issued ``resume`` ControlFrame (Plan 6 D6.11)."""

    req_id: str
    ok: bool
    error: NotRequired[str]


EventFrame = (
    InstallDoneFrame
    | InstallErrorFrame
    | MsgChunkFrame
    | ToolRequestFrame
    | ToolResultFrame
    | SessionEndFrame
    | ErrorFrame
    | PongFrame
    | PauseAckFrame
    | ResumeAckFrame
)


# ── Control frames (host → runner) ────────────────────────────────────────

class ToolDecisionFrame(_BaseFrame):
    req_id: str
    # needs_hitl is host-internal state; never serialized to wire
    decision: Literal["accept", "deny"]
    reason: NotRequired[str]


class InterruptFrame(_BaseFrame):
    pass


class ShutdownFrame(_BaseFrame):
    pass


class PingFrame(_BaseFrame):
    pass


class PauseFrame(_BaseFrame):
    """Host → runner pause directive (Plan 6 D6.11).

    The runner's control loop calls ``ClaudeSDKClient.interrupt()`` and
    emits a :class:`PauseAckFrame` with the matching ``req_id``.
    """

    req_id: str
    reason: NotRequired[str]


class ResumeFrame(_BaseFrame):
    """Host → runner resume directive (Plan 6 D6.11).

    The runner's control loop calls ``ClaudeSDKClient.query(hint or
    "continue")`` and emits a :class:`ResumeAckFrame` with the matching
    ``req_id``.
    """

    req_id: str
    hint: NotRequired[str]


ControlFrame = (
    ToolDecisionFrame
    | InterruptFrame
    | ShutdownFrame
    | PingFrame
    | PauseFrame
    | ResumeFrame
)


# ── Exceptions ────────────────────────────────────────────────────────────

class TransportClosed(Exception):
    """Raised when send/recv is called on a closed transport."""


# ── Protocol ──────────────────────────────────────────────────────────────

@runtime_checkable
class SessionTransport(Protocol):
    """Bidirectional JSONL stream. Single connection, long-lived.

    Signatures declare the HOST-SIDE view (send=ControlFrame, recv=EventFrame).
    Runner-side implementations re-use this Protocol with `# type: ignore[override]`
    on send/recv; the pipe is symmetric at runtime — each side reads what the other
    side writes.
    """

    @property
    def is_alive(self) -> bool: ...
    async def send(self, frame: ControlFrame) -> None: ...
    async def recv(self) -> EventFrame: ...
    async def close(self) -> None: ...
