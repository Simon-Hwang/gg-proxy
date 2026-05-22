"""Public surface for gg_relay.session.

Stable primitives consumed by SessionManager (Plan 4), CLI, and external
callers. Internals (_PendingEntry, _CLOSE_SENTINEL, _envelope, etc.) are
NOT exported.
"""
from gg_relay.session.client import make_sdk_runner, make_wire_runner
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.executor.protocol import ExecutorBackend, RunnerFn
from gg_relay.session.hitl.coordinator import HITLCoordinator, HITLNotPending
from gg_relay.session.hitl.policy import DEFAULT_POLICY, ToolPolicy
from gg_relay.session.manager import (
    ExecutorFactory,
    MaxPausedExceeded,
    ResumeQueueTimeout,
    SessionDetail,
    SessionManager,
    SessionNotFound,
    SessionNotPaused,
    SessionNotRunning,
    make_inprocess_factory,
)
from gg_relay.session.spec import (
    Decision,
    PluginManifest,
    RuntimeHandle,
    SessionRuntimeContext,
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
    "ExecutorFactory",
    "HITLCoordinator",
    "HITLNotPending",
    "InProcessExecutor",
    "MaxPausedExceeded",
    "PluginManifest",
    "ResumeQueueTimeout",
    "RunnerFn",
    "RuntimeHandle",
    "SessionDetail",
    "SessionManager",
    "SessionNotFound",
    "SessionNotPaused",
    "SessionNotRunning",
    "SessionRuntimeContext",
    "SessionSpec",
    "SessionTransport",
    "ToolPolicy",
    "TransportClosed",
    "make_inprocess_factory",
    "make_sdk_runner",
    "make_wire_runner",
]
