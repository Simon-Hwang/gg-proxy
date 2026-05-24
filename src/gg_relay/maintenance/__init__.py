"""Periodic maintenance helpers (Plan 8 Task 20 / D8.3).

Provides batched retention pruning for the long-lived observability
tables (``events``, ``audit_log``, ``hitl_requests``). The CLI command
``gg-relay maintenance`` is the canonical entry point; production
deployments wire it to cron / systemd timer.
"""
from __future__ import annotations

from gg_relay.maintenance.retention import (
    RetentionResult,
    RetentionSummary,
    run_retention,
)

__all__ = [
    "RetentionResult",
    "RetentionSummary",
    "run_retention",
]
