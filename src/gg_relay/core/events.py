"""Typed ``RelayEvent`` hierarchy + frame → event dispatch table.

This module is the Plan 5 D5.11=B replacement for the dict-shaped fan-out
that ``SessionManager`` and ``EventBus`` have been using since Plan 1. Each
publisher (SessionManager, WireBridge drains, future IM/SSE subscribers)
now constructs a concrete dataclass subclass; the bus routes by class name
so subscribers can filter with either :class:`type` literals or string
class-names (D5.2=A3 — see :class:`gg_relay.core.event_bus.EventBus`).

All events are :func:`dataclass(frozen=True, slots=True)` so they are
hashable, immutable, and cheap. The :data:`DeliveryTier` literal is the
subscriber-queue hint (D5.3 reframing): ``"lossy"`` = drop oldest on full
buffer; ``"durable"`` = block / backpressure publisher. Persistence is
already handled by the SessionManager pipeline *before* publish — the tier
is **not** a "write to DB before fan-out" contract anymore.

The 11 concrete subclasses (vs the 6 in PLAN.md §8) cover the entire
EventFrame surface from ``session/transport/protocol.py`` so Plan 6 won't
have to add new types later. The frame → event dispatch table
:data:`_FRAME_TO_EVENT` is the single source of truth that
``SessionManager._persist_frame`` uses to lift dict frames into typed
events at the fan-out boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, get_args
from uuid import UUID, uuid4

__all__ = [
    "DeliveryTier",
    "Heartbeat",
    "HITLRequested",
    "HITLResolved",
    "InstallDone",
    "InstallError",
    "KeyInvalidated",
    "RelayEvent",
    "RelayEventT",
    "SessionCompleted",
    "SessionCreated",
    "SessionOutputChunk",
    "SessionStateChanged",
    "ToolRequested",
    "ToolResolved",
    "frame_to_event",
]


DeliveryTier = Literal["lossy", "durable"]
"""Subscriber-queue backpressure hint, see module docstring."""


# ── Root + 11 concrete subclasses ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RelayEvent:
    """Root of the relay event hierarchy.

    Subclasses MUST add their own fields with defaults (every dataclass
    field added after a defaulted field must itself have a default — the
    base ``event_id``/``occurred_at``/``delivery_tier`` are all defaulted
    so subclasses follow suit).
    """

    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    delivery_tier: DeliveryTier = "lossy"


@dataclass(frozen=True, slots=True)
class SessionCreated(RelayEvent):
    """A session row has been written; the manager is about to spawn it."""

    session_id: str = ""
    prompt_redacted: str = ""
    tags: tuple[str, ...] = ()
    delivery_tier: DeliveryTier = "durable"


@dataclass(frozen=True, slots=True)
class SessionStateChanged(RelayEvent):
    """A session moved between lifecycle states.

    ``to_state`` is the new :class:`gg_relay.core.SessionState` value; the
    OTel subscriber relies on the first ``running`` / first terminal
    transition to open / close the per-session span.
    """

    session_id: str = ""
    from_state: str = ""
    to_state: str = ""
    reason: str | None = None
    delivery_tier: DeliveryTier = "durable"


@dataclass(frozen=True, slots=True)
class SessionOutputChunk(RelayEvent):
    """Wraps an SDK ``msg.chunk`` frame. Lossy by design — UI catch-up OK."""

    session_id: str = ""
    seq: int = 0
    frame_type: str = "msg.chunk"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionCompleted(RelayEvent):
    """Terminal session event with token / cost summary."""

    session_id: str = ""
    status: Literal["completed", "failed", "cancelled"] = "completed"
    tokens: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    delivery_tier: DeliveryTier = "durable"


@dataclass(frozen=True, slots=True)
class HITLRequested(RelayEvent):
    """A tool call needs human-in-the-loop approval."""

    session_id: str = ""
    req_id: str = ""
    tool: str = ""
    args_redacted: dict[str, Any] = field(default_factory=dict)
    delivery_tier: DeliveryTier = "durable"


@dataclass(frozen=True, slots=True)
class HITLResolved(RelayEvent):
    """The operator (or dashboard) decided a HITL request."""

    session_id: str = ""
    req_id: str = ""
    decision: Literal["accept", "deny"] = "accept"
    reason: str | None = None
    resolver: str | None = None
    delivery_tier: DeliveryTier = "durable"


@dataclass(frozen=True, slots=True)
class ToolRequested(RelayEvent):
    """Wraps the wire-level ``tool.request`` frame.

    Distinct from :class:`HITLRequested`: this fires for *every* tool call
    (auto-accept or not). HITLRequested is only published when the policy
    returns ``NEEDS_HITL`` and we're waiting on an operator.
    """

    session_id: str = ""
    seq: int = 0
    req_id: str = ""
    tool: str = ""
    args_redacted: dict[str, Any] = field(default_factory=dict)
    delivery_tier: DeliveryTier = "durable"


@dataclass(frozen=True, slots=True)
class ToolResolved(RelayEvent):
    """Wraps the wire-level ``tool.result`` frame."""

    session_id: str = ""
    seq: int = 0
    req_id: str = ""
    ok: bool = True
    result_redacted: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    delivery_tier: DeliveryTier = "durable"


@dataclass(frozen=True, slots=True)
class InstallDone(RelayEvent):
    """Plugin assembler finished; ``modules`` is the actually-installed set."""

    session_id: str = ""
    profile_id: str | None = None
    modules: tuple[str, ...] = ()
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class InstallError(RelayEvent):
    """Plugin assembler failed or runner emitted an ``error`` frame."""

    session_id: str = ""
    seq: int = 0
    code: str = ""
    message: str = ""
    traceback: str | None = None
    delivery_tier: DeliveryTier = "durable"


@dataclass(frozen=True, slots=True)
class Heartbeat(RelayEvent):
    """Runner liveness ping. Lossy by default — published frequently."""

    session_id: str = ""
    runtime_id: str = ""


@dataclass(frozen=True, slots=True)
class KeyInvalidated(RelayEvent):
    """Plan 9 D9.10 — dashboard internal key rotation broadcast.

    Published by :class:`gg_relay.store.dashboard_keys.DashboardKeyStore`
    rotate / delete operations (typically triggered by an admin endpoint
    or the ``gg-relay dashboard-rotate`` CLI). Multi-worker subscribers
    (:class:`gg_relay.cluster.key_invalidate.KeyInvalidateSubscriber`)
    reload their ``app.state.dashboard_internal_keys`` from the DB on
    receipt so the rotation takes effect across the cluster without
    requiring every pod to restart.

    ``usernames`` is a tuple so the broadcast can carry a bulk
    rotation (e.g. operator removes 3 dashboard users at once);
    subscribers refresh the entire mapping rather than diffing per
    username — keeps the consistency guarantee simple.

    ``session_id`` is empty (this isn't a session-scoped event); the
    field is required by the :class:`RelayEvent` filter contract for
    the SSE per-session feed which simply ignores events with no
    matching session_id.
    """

    session_id: str = ""
    usernames: tuple[str, ...] = ()
    delivery_tier: DeliveryTier = "durable"


# ── Union for static typing of subscribers ───────────────────────────────


RelayEventT = (
    SessionCreated
    | SessionStateChanged
    | SessionOutputChunk
    | SessionCompleted
    | HITLRequested
    | HITLResolved
    | ToolRequested
    | ToolResolved
    | InstallDone
    | InstallError
    | Heartbeat
    | KeyInvalidated
)
"""Union over every concrete subclass.

Use this as the static annotation for subscriber handlers / publisher
return types. ``get_args(RelayEventT)`` returns the 11 concrete subclasses
in declaration order; the test suite asserts this against the module's
``__all__`` listing so a forgotten subclass fails CI.
"""


# ── Frame → event dispatch table ──────────────────────────────────────────


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _from_msg_chunk(sid: str, payload: dict[str, Any]) -> SessionOutputChunk:
    return SessionOutputChunk(
        session_id=sid,
        seq=_safe_int(payload.get("seq")),
        frame_type="msg.chunk",
        payload=dict(payload),
    )


def _from_tool_request(sid: str, payload: dict[str, Any]) -> ToolRequested:
    return ToolRequested(
        session_id=sid,
        seq=_safe_int(payload.get("seq")),
        req_id=_safe_str(payload.get("req_id")),
        tool=_safe_str(payload.get("tool")),
        args_redacted=dict(payload.get("args") or {}),
    )


def _from_tool_result(sid: str, payload: dict[str, Any]) -> ToolResolved:
    return ToolResolved(
        session_id=sid,
        seq=_safe_int(payload.get("seq")),
        req_id=_safe_str(payload.get("req_id")),
        ok=bool(payload.get("ok", True)),
        result_redacted=dict(payload.get("result") or {}),
        error=payload.get("error") if isinstance(payload.get("error"), str) else None,
    )


def _from_install_done(sid: str, payload: dict[str, Any]) -> InstallDone:
    modules_raw = payload.get("modules") or ()
    return InstallDone(
        session_id=sid,
        profile_id=payload.get("profile_id") if isinstance(
            payload.get("profile_id"), str
        ) else None,
        modules=tuple(m for m in modules_raw if isinstance(m, str)),
        duration_ms=_safe_int(payload.get("duration_ms")),
    )


def _from_install_error(sid: str, payload: dict[str, Any]) -> InstallError:
    return InstallError(
        session_id=sid,
        seq=_safe_int(payload.get("seq")),
        code=_safe_str(payload.get("code"), "install_failed"),
        message=_safe_str(payload.get("message")),
    )


def _from_error_frame(sid: str, payload: dict[str, Any]) -> InstallError:
    """``error`` frames map onto :class:`InstallError` until Plan 6 splits.

    Plan 5 keeps a single error class to avoid proliferating subtypes; the
    ``code`` field disambiguates installer vs runtime errors so subscribers
    that care can filter on it.
    """
    return InstallError(
        session_id=sid,
        seq=_safe_int(payload.get("seq")),
        code=_safe_str(payload.get("code"), "runtime_error"),
        message=_safe_str(payload.get("message")),
        traceback=payload.get("traceback") if isinstance(
            payload.get("traceback"), str
        ) else None,
    )


def _from_session_end(sid: str, payload: dict[str, Any]) -> SessionCompleted:
    status_raw = _safe_str(payload.get("status"), "completed")
    status: Literal["completed", "failed", "cancelled"]
    if status_raw == "cancelled":
        status = "cancelled"
    elif status_raw in {"failed", "crashed"}:
        status = "failed"
    else:
        status = "completed"
    tokens_raw = payload.get("tokens") or {}
    tokens: dict[str, int] = {}
    if isinstance(tokens_raw, dict):
        for k, v in tokens_raw.items():
            if isinstance(k, str):
                tokens[k] = _safe_int(v)
    return SessionCompleted(
        session_id=sid,
        status=status,
        tokens=tokens,
        cost_usd=float(payload.get("cost_usd") or 0.0),
    )


def _from_pong(sid: str, payload: dict[str, Any]) -> Heartbeat:
    return Heartbeat(
        session_id=sid,
        runtime_id=_safe_str(payload.get("runtime_id")),
    )


_FRAME_TO_EVENT: dict[str, Any] = {
    "msg.chunk": _from_msg_chunk,
    "tool.request": _from_tool_request,
    "tool.result": _from_tool_result,
    "install.done": _from_install_done,
    "install.error": _from_install_error,
    "error": _from_error_frame,
    "session.end": _from_session_end,
    "pong": _from_pong,
}
"""Frame ``type`` string → factory function ``(session_id, payload) -> RelayEvent``.

Covers every concrete subclass of
:class:`gg_relay.session.transport.protocol.EventFrame` (the 8 wire-level
frame variants); ``SessionCreated`` and ``SessionStateChanged`` come from
the SessionManager itself (no wire frame counterpart) and are constructed
directly by the manager.
"""


def frame_to_event(session_id: str, frame: dict[str, Any]) -> RelayEventT | None:
    """Lift a wire-level frame dict to its typed :class:`RelayEvent`.

    Returns ``None`` when the frame's ``type`` has no registered factory;
    callers (SessionManager._persist_frame) should treat ``None`` as
    "publish a generic chunk" / log + skip and never crash on unknown
    types — Plan 8+ may add wire frame variants that older subscribers
    don't recognise.
    """
    ftype = frame.get("type")
    if not isinstance(ftype, str):
        return None
    factory = _FRAME_TO_EVENT.get(ftype)
    if factory is None:
        return None
    return factory(session_id, frame)  # type: ignore[no-any-return]


def _all_subclasses() -> tuple[type[RelayEvent], ...]:
    """Helper used by the test suite to enforce ``__all__`` ↔ ``RelayEventT`` parity."""
    return tuple(get_args(RelayEventT))
