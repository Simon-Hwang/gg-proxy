"""Bidirectional JSONL transport between host and runner.

InMemoryTransport is for in-process backend; UnixSocketTransport (Plan 3) is for
Docker. Both implement SessionTransport Protocol.
"""
from gg_relay.session.transport.inmemory import InMemoryTransport, make_pair
from gg_relay.session.transport.protocol import (
    ControlFrame,
    EventFrame,
    SessionTransport,
    TransportClosed,
)
from gg_relay.session.transport.unixsocket import (
    UnixSocketServer,
    UnixSocketTransport,
)

__all__ = [
    "ControlFrame",
    "EventFrame",
    "InMemoryTransport",
    "SessionTransport",
    "TransportClosed",
    "UnixSocketServer",
    "UnixSocketTransport",
    "make_pair",
]
