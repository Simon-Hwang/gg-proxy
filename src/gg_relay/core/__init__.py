"""Core primitives (zero external deps)."""
from gg_relay.core.domain import TERMINAL_STATES, SessionState, SessionSummary
from gg_relay.core.event_bus import EventBus
from gg_relay.core.events import (
    DeliveryTier,
    Heartbeat,
    HITLRequested,
    HITLResolved,
    InstallDone,
    InstallError,
    RelayEvent,
    RelayEventT,
    SessionCompleted,
    SessionCreated,
    SessionOutputChunk,
    SessionStateChanged,
    ToolRequested,
    ToolResolved,
    frame_to_event,
)

__all__ = [
    "TERMINAL_STATES",
    "DeliveryTier",
    "EventBus",
    "Heartbeat",
    "HITLRequested",
    "HITLResolved",
    "InstallDone",
    "InstallError",
    "RelayEvent",
    "RelayEventT",
    "SessionCompleted",
    "SessionCreated",
    "SessionOutputChunk",
    "SessionState",
    "SessionStateChanged",
    "SessionSummary",
    "ToolRequested",
    "ToolResolved",
    "frame_to_event",
]
