"""Public surface for gg_relay.session.

Stable primitives consumed by SessionManager (Plan 4), CLI, and external
callers. Internals (_PendingEntry, _CLOSE_SENTINEL, _envelope, etc.) are
NOT exported.
"""
from gg_relay.session.client import make_sdk_runner
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.executor.protocol import ExecutorBackend, RunnerFn
from gg_relay.session.hitl.coordinator import HITLCoordinator, HITLNotPending
from gg_relay.session.hitl.policy import DEFAULT_POLICY, ToolPolicy
from gg_relay.session.spec import (
    Decision,
    PluginManifest,
    RuntimeHandle,
    SessionSpec,
)
from gg_relay.session.transport.protocol import (
    ControlFrame,
    EventFrame,
    SessionTransport,
    TransportClosed,
)

__all__ = [
    "DEFAULT_POLICY",
    "ControlFrame",
    "Decision",
    "EventFrame",
    "ExecutorBackend",
    "HITLCoordinator",
    "HITLNotPending",
    "InProcessExecutor",
    "PluginManifest",
    "RunnerFn",
    "RuntimeHandle",
    "SessionSpec",
    "SessionTransport",
    "ToolPolicy",
    "TransportClosed",
    "make_sdk_runner",
]
