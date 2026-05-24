"""Plan 9 — cluster scaling utilities.

This package gathers the multi-worker / K8s machinery shipped in
Plan 9. v0.9.0-rc carries D9.11 only (boot-time deployment-mode
safety check); v0.9.1 will add RedisStreamEventBus,
RedisRateLimitStore, KeyInvalidateSubscriber, and the K8s runbook
helpers.
"""
from __future__ import annotations

from gg_relay.cluster.boot_check import (
    MULTI_WORKER_SAFE_BACKENDS,
    DeploymentModeError,
    validate_deployment_mode,
)

__all__ = [
    "MULTI_WORKER_SAFE_BACKENDS",
    "DeploymentModeError",
    "validate_deployment_mode",
]
