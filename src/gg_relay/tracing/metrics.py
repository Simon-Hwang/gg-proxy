"""Prometheus metrics registry (Plan 5 Task 6 / D5.5=A).

Direct ``prometheus-client`` usage (no OTel exporter), per D5.5=A: keeps
the metrics surface small and avoids requiring the OTel SDK metrics
pipeline. The registry is process-wide and shared between collectors
(``OtelSubscriber`` increments counters; the SessionManager can call
:func:`record_session_submitted` etc. directly).

Counters / gauges chosen to match PLAN.md §10:

  gg_relay_sessions_total{}                 — Counter (every submit)
  gg_relay_sessions_by_status_total{status}  — Counter (terminal status)
  gg_relay_sessions_active                   — Gauge (live concurrent)
  gg_relay_session_state_changes_total{state} — Counter
  gg_relay_hitl_requests_total              — Counter
  gg_relay_hitl_resolved_total{decision}     — Counter
  gg_relay_tokens_input_total               — Counter
  gg_relay_tokens_output_total              — Counter
  gg_relay_cost_usd_total                   — Counter
  gg_relay_bus_drops_total                  — Counter (lossy drops)
  gg_relay_bus_durable_drops_total          — Counter (post-timeout drops)
  gg_relay_session_duration_seconds         — Histogram
  gg_relay_errors_total{kind}               — Counter
"""
from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

__all__ = [
    "CONTENT_TYPE_LATEST",
    "REGISTRY",
    "render",
    "BUS_DROPS",
    "BUS_DURABLE_DROPS",
    "COST_USD",
    "ERRORS",
    "HITL_REQUESTS",
    "HITL_RESOLVED",
    "SESSIONS_ACTIVE",
    "SESSIONS_BY_STATUS",
    "SESSIONS_TOTAL",
    "SESSION_DURATION",
    "SESSION_STATE_CHANGES",
    "TOKENS_INPUT",
    "TOKENS_OUTPUT",
]


REGISTRY = CollectorRegistry()

SESSIONS_TOTAL = Counter(
    "gg_relay_sessions_total",
    "Sessions submitted to gg-relay.",
    registry=REGISTRY,
)
SESSIONS_BY_STATUS = Counter(
    "gg_relay_sessions_by_status_total",
    "Sessions that reached a terminal status.",
    labelnames=("status",),
    registry=REGISTRY,
)
SESSIONS_ACTIVE = Gauge(
    "gg_relay_sessions_active",
    "Sessions currently in RUNNING state.",
    registry=REGISTRY,
)
SESSION_STATE_CHANGES = Counter(
    "gg_relay_session_state_changes_total",
    "State transitions emitted by SessionManager.",
    labelnames=("state",),
    registry=REGISTRY,
)
HITL_REQUESTS = Counter(
    "gg_relay_hitl_requests_total",
    "HITL requests issued (NEEDS_HITL policy verdict).",
    registry=REGISTRY,
)
HITL_RESOLVED = Counter(
    "gg_relay_hitl_resolved_total",
    "HITL requests resolved.",
    labelnames=("decision",),
    registry=REGISTRY,
)
TOKENS_INPUT = Counter(
    "gg_relay_tokens_input_total",
    "Input tokens consumed by SDK runs (per SessionCompleted).",
    registry=REGISTRY,
)
TOKENS_OUTPUT = Counter(
    "gg_relay_tokens_output_total",
    "Output tokens emitted by SDK runs.",
    registry=REGISTRY,
)
COST_USD = Counter(
    "gg_relay_cost_usd_total",
    "Cumulative cost in USD (per SessionCompleted.cost_usd).",
    registry=REGISTRY,
)
BUS_DROPS = Counter(
    "gg_relay_bus_drops_total",
    "Events dropped by the EventBus due to a slow subscriber (lossy tier).",
    registry=REGISTRY,
)
BUS_DURABLE_DROPS = Counter(
    "gg_relay_bus_durable_drops_total",
    "Durable events dropped after exceeding the publisher block timeout.",
    registry=REGISTRY,
)
SESSION_DURATION = Histogram(
    "gg_relay_session_duration_seconds",
    "End-to-end session duration (submit → terminal state).",
    buckets=(1, 5, 15, 60, 300, 1800, 7200),
    registry=REGISTRY,
)
ERRORS = Counter(
    "gg_relay_errors_total",
    "Errors recorded by gg-relay (install / runtime).",
    labelnames=("kind",),
    registry=REGISTRY,
)


def render() -> tuple[bytes, str]:
    """Return ``(body_bytes, content_type)`` suitable for a FastAPI response."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
