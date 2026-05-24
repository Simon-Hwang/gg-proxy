"""Plan 9 D9.11 — multi-worker boot-time safety check.

Validates at lifespan startup that the running Config is internally
consistent with the declared :attr:`Config.deployment_mode`. The
check exists to defuse a silent multi-worker failure mode:

    operator sets ``replicas: 3`` in helm/k8s
    BUT forgets to flip ``event_bus_backend`` or
    ``rate_limit_backend`` from ``inmemory`` → ``redis``
    → each worker runs its own private bus / bucket table
    → SSE delivery is non-deterministic (worker A publishes,
      worker B subscribes, event lost)
    → rate limit becomes per-worker (3× allowed traffic)
    → dashboard cookies signed on worker A 401 on worker B

Pre-production simplification (v0.9.0): the check is **always
fail-fast** when violations are found. The ``deployment_mode_strict``
warn-only escape hatch was removed because gg-relay has no installed
userbase that needs a phased rollout — strict-from-day-one stops
operators from shipping silently-broken multi-worker configs.

Data-driven design (Santa Round 3 Reviewer D MAJOR #11): the set of
acceptable backends is exposed as :data:`MULTI_WORKER_SAFE_BACKENDS`
so future plans (Kafka, NATS) can opt in without modifying the
check function. Adding a new backend = add a literal to
:attr:`Config.event_bus_backend` Literal type AND insert the literal
into this set.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gg_relay.config import Config

logger = logging.getLogger("gg_relay.cluster.boot_check")

MULTI_WORKER_SAFE_BACKENDS: frozenset[str] = frozenset({"redis"})


class DeploymentModeError(RuntimeError):
    """Raised when a multi-worker config violation is detected at boot."""


def validate_deployment_mode(cfg: Config) -> list[str]:
    """Inspect ``cfg`` and return the list of multi-worker violations.

    Behaviour by mode:

    * ``cfg.deployment_mode == "single_worker"`` — returns ``[]``
      unconditionally. Single-worker is the default and every
      backend value is acceptable.
    * ``cfg.deployment_mode == "multi_worker"`` — validates that
      both ``event_bus_backend`` and ``rate_limit_backend`` are in
      :data:`MULTI_WORKER_SAFE_BACKENDS`, and that ``redis_url`` is
      set when either backend selects redis. **Raises**
      :class:`DeploymentModeError` (always strict) if any
      violations are present.

    The returned list is empty on success — callers MAY set a
    Prometheus gauge to ``len(violations)`` for observability, but
    the lifespan never sees a non-empty list because the raise
    short-circuits.
    """
    if cfg.deployment_mode != "multi_worker":
        return []

    violations: list[str] = []

    event_bus = cfg.event_bus_backend
    if event_bus not in MULTI_WORKER_SAFE_BACKENDS:
        violations.append(
            f"event_bus_backend={event_bus!r} is not multi-worker "
            f"safe; expected one of {sorted(MULTI_WORKER_SAFE_BACKENDS)}. "
            "Set RELAY_EVENT_BUS_BACKEND=redis (or another safe "
            "backend) for true cross-worker fan-out."
        )

    rate_limit = cfg.rate_limit_backend
    if rate_limit not in MULTI_WORKER_SAFE_BACKENDS:
        violations.append(
            f"rate_limit_backend={rate_limit!r} is not multi-worker "
            f"safe; expected one of {sorted(MULTI_WORKER_SAFE_BACKENDS)}. "
            "Per-worker buckets allow `replicas × rate_per_min` total "
            "traffic — set RELAY_RATE_LIMIT_BACKEND=redis for shared "
            "buckets."
        )

    if (event_bus == "redis" or rate_limit == "redis") and not cfg.redis_url:
        violations.append(
            "redis_url is unset but at least one backend selects "
            "'redis'. Set RELAY_REDIS_URL=redis://... (or "
            "rediss:// for TLS in production)."
        )

    if violations:
        joined = "\n  - ".join(violations)
        for v in violations:
            logger.error("multi_worker_config_violation: %s", v)
        raise DeploymentModeError(
            "Multi-worker deployment configuration violations:\n  - "
            f"{joined}\nSee docs/cluster.md for the recommended values."
        )

    return violations
