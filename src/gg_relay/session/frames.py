"""Typed event-frame builders.

One ``make_xxx(...)`` function per event-frame type. Builders return
``TypedDict`` instances (via ``cast``) so call sites can rely on key shape
without scattering literal-dict construction across the codebase.

Sequence numbering note (carried over from client.py): ``seq`` is monotonic
but **not gapless** — the caller is responsible for incrementing.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast

from gg_relay.session.plugins.protocol import InstallReport
from gg_relay.session.transport.protocol import (
    ErrorFrame,
    InstallDoneFrame,
    InstallErrorFrame,
    MsgChunkFrame,
    PauseAckFrame,
    PauseFrame,
    PingFrame,
    PongFrame,
    ResumeAckFrame,
    ResumeFrame,
    SessionEndFrame,
    ShutdownFrame,
    ToolDecisionFrame,
    ToolRequestFrame,
    ToolResultFrame,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _envelope(seq: int, type_: str, **rest: Any) -> dict[str, Any]:
    """Internal: shared base-frame fields (``v`` / ``type`` / ``seq`` / ``ts``)
    plus payload kwargs."""
    return {"v": 1, "type": type_, "seq": seq, "ts": _now_iso(), **rest}


def make_msg_chunk(seq: int, data: dict[str, Any]) -> MsgChunkFrame:
    return cast(MsgChunkFrame, _envelope(seq, "msg.chunk", data=data))


def make_tool_request(
    seq: int, req_id: str, tool: str, args: dict[str, Any]
) -> ToolRequestFrame:
    return cast(
        ToolRequestFrame,
        _envelope(seq, "tool.request", req_id=req_id, tool=tool, args=args),
    )


def make_tool_result(
    seq: int, req_id: str, ok: bool, result: dict[str, Any]
) -> ToolResultFrame:
    return cast(
        ToolResultFrame,
        _envelope(seq, "tool.result", req_id=req_id, ok=ok, result=result),
    )


def make_session_end(
    seq: int,
    status: Literal["completed", "cancelled", "crashed"],
    *,
    tokens: dict[str, Any],
    cost_usd: float,
) -> SessionEndFrame:
    return cast(
        SessionEndFrame,
        _envelope(
            seq, "session.end", status=status, tokens=tokens, cost_usd=cost_usd
        ),
    )


def make_error(
    seq: int, code: str, message: str, *, traceback_: str | None = None
) -> ErrorFrame:
    payload: dict[str, Any] = {"code": code, "message": message}
    if traceback_ is not None:
        payload["traceback"] = traceback_
    return cast(ErrorFrame, _envelope(seq, "error", **payload))


def make_install_done(seq: int, report: InstallReport) -> InstallDoneFrame:
    """Build an install.done frame from a successful InstallReport."""
    return cast(
        InstallDoneFrame,
        _envelope(
            seq,
            "install.done",
            profile_id=report.profile_id,
            modules=list(report.selected_modules),
            duration_ms=report.duration_ms,
            install_root=str(report.install_root),
        ),
    )


_STDERR_TAIL_MAX = 2048


def make_install_error(
    seq: int, code: str, message: str, *, stderr_tail: str = ""
) -> InstallErrorFrame:
    """Build an install.error frame; stderr_tail is right-truncated to 2 KiB
    so a runaway installer can't blow up the transport buffer."""
    return cast(
        InstallErrorFrame,
        _envelope(
            seq,
            "install.error",
            code=code,
            message=message,
            stderr_tail=stderr_tail[-_STDERR_TAIL_MAX:],
        ),
    )


def make_tool_decision(
    seq: int,
    req_id: str,
    decision: Literal["accept", "deny"],
    *,
    reason: str | None = None,
) -> ToolDecisionFrame:
    """Host → runner ControlFrame. Reason is dropped if None to keep wire
    payload small."""
    payload: dict[str, Any] = {"req_id": req_id, "decision": decision}
    if reason is not None:
        payload["reason"] = reason
    return cast(ToolDecisionFrame, _envelope(seq, "tool.decision", **payload))


def make_ping(seq: int) -> PingFrame:
    """Host → runner heartbeat probe (D3.10)."""
    return cast(PingFrame, _envelope(seq, "ping"))


def make_pong(seq: int) -> PongFrame:
    """Runner → host heartbeat reply (D3.10)."""
    return cast(PongFrame, _envelope(seq, "pong"))


def make_shutdown(seq: int) -> ShutdownFrame:
    """Host → runner graceful-stop signal (D3.12). seq=-1 by convention when
    the bridge is racing teardown and has no monotonic counter handy."""
    return cast(ShutdownFrame, _envelope(seq, "shutdown"))


def make_pause(seq: int, req_id: str, *, reason: str | None = None) -> PauseFrame:
    """Host → runner pause directive (Plan 6 D6.11).

    ``req_id`` MUST be unique per outstanding pause/resume request so the
    runner's ack carries an unambiguous correlation key. ``reason`` is
    persisted into the resulting ``SessionStateChanged.reason`` upstream.
    """
    payload: dict[str, Any] = {"req_id": req_id}
    if reason is not None:
        payload["reason"] = reason
    return cast(PauseFrame, _envelope(seq, "pause", **payload))


def make_resume(seq: int, req_id: str, *, hint: str | None = None) -> ResumeFrame:
    """Host → runner resume directive (Plan 6 D6.11)."""
    payload: dict[str, Any] = {"req_id": req_id}
    if hint is not None:
        payload["hint"] = hint
    return cast(ResumeFrame, _envelope(seq, "resume", **payload))


def make_pause_ack(
    seq: int, req_id: str, *, ok: bool, error: str | None = None
) -> PauseAckFrame:
    """Runner → host pause ack (Plan 6 D6.11). ``error`` populated iff
    ``ok=False`` (e.g. SDK rejected ``interrupt()``)."""
    payload: dict[str, Any] = {"req_id": req_id, "ok": ok}
    if error is not None:
        payload["error"] = error
    return cast(PauseAckFrame, _envelope(seq, "pause.ack", **payload))


def make_resume_ack(
    seq: int, req_id: str, *, ok: bool, error: str | None = None
) -> ResumeAckFrame:
    """Runner → host resume ack (Plan 6 D6.11)."""
    payload: dict[str, Any] = {"req_id": req_id, "ok": ok}
    if error is not None:
        payload["error"] = error
    return cast(ResumeAckFrame, _envelope(seq, "resume.ack", **payload))
