"""Core primitives (zero external deps)."""
from gg_relay.core.domain import TERMINAL_STATES, SessionState, SessionSummary
from gg_relay.core.event_bus import EventBus

__all__ = [
    "TERMINAL_STATES",
    "EventBus",
    "SessionState",
    "SessionSummary",
]
