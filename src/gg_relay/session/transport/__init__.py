"""Bidirectional JSONL transport between host and runner.

InMemoryTransport is for in-process backend; UnixSocketTransport (Plan 3) is for Docker.
Both implement SessionTransport Protocol.
"""
from gg_relay.session.transport.protocol import (
    ControlFrame,
    EventFrame,
    SessionTransport,
    TransportClosed,
)

__all__ = ["ControlFrame", "EventFrame", "SessionTransport", "TransportClosed"]
