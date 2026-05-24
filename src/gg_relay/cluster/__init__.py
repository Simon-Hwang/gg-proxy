"""Plan 9 — cluster scaling utilities.

This package gathers the multi-worker / K8s machinery shipped in
Plan 9. v0.9.0 carries the full surface:

* :mod:`boot_check` — multi-worker config safety check (D9.11)
* :mod:`wire` — Redis stream wire schema v1 (D9.13)
* :mod:`redis_bus` — :class:`RedisStreamEventBus` (D9.1)
* :mod:`redis_rate_limit` — :class:`RedisRateLimitStore` (D9.2)
* :mod:`key_invalidate` — :class:`KeyInvalidateSubscriber` (D9.10)
* :mod:`drain` — admin drain endpoint (D9.12)
* :mod:`k8s_executor` — :class:`K8sJobExecutor` (D9.8)
"""
from __future__ import annotations

from gg_relay.cluster.boot_check import (
    MULTI_WORKER_SAFE_BACKENDS,
    DeploymentModeError,
    validate_deployment_mode,
)
from gg_relay.cluster.redis_bus import RedisStreamEventBus
from gg_relay.cluster.redis_rate_limit import RedisRateLimitStore
from gg_relay.cluster.wire import (
    SCHEMA_VERSION,
    STREAM_KEY,
    UnsupportedWireVersionError,
    decode_event,
    encode_event,
)

__all__ = [
    "MULTI_WORKER_SAFE_BACKENDS",
    "SCHEMA_VERSION",
    "STREAM_KEY",
    "DeploymentModeError",
    "RedisRateLimitStore",
    "RedisStreamEventBus",
    "UnsupportedWireVersionError",
    "decode_event",
    "encode_event",
    "validate_deployment_mode",
]
