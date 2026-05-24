"""Plan 9 v0.9.0-rc D9.11 — multi-worker boot-time safety check.

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

The check is **warn-only by default** because forcing fail-fast in
v0.9.0-rc would brick anyone who had ``RELAY_DEPLOYMENT_MODE=
multi_worker`` set without realising the Redis prerequisites.
Operators set :attr:`Config.deployment_mode_strict` to ``True`` once
they've completed the v0.9.1 D9.12 upgrade runbook.

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

# Set of backend selector values considered safe for multi-worker
# deployments. Extend when new backends are added to
# :attr:`Config.event_bus_backend` / :attr:`Config.rate_limit_backend`.
#
# As of v0.9.0-rc: only ``"redis"`` is safe. ``"inmemory"`` is
# explicitly NOT in the set — the in-process EventBus / Token bucket
# table cannot be shared across workers, so multi-worker + inmemory
# is the silent-failure case this check defuses.
MULTI_WORKER_SAFE_BACKENDS: frozenset[str] = frozenset({"redis"})


class DeploymentModeError(RuntimeError):
    """Raised when :attr:`Config.deployment_mode_strict` is True and a
    multi-worker config violation is detected at boot time."""


def validate_deployment_mode(cfg: Config) -> list[str]:
    """Inspect ``cfg`` and return the list of multi-worker violations.

    Behaviour by mode:

    * ``cfg.deployment_mode == "single_worker"`` — returns ``[]``
      unconditionally. Single-worker is the default and every
      backend value is acceptable.
    * ``cfg.deployment_mode == "multi_worker"`` — validates that
      both ``event_bus_backend`` and ``rate_limit_backend`` are in
      :data:`MULTI_WORKER_SAFE_BACKENDS`, and that ``redis_url`` is
      set when either backend selects redis. Returns the list of
      human-readable violation strings (empty when configuration is
      cluster-safe).

    Side effects:

    * On violations + ``cfg.deployment_mode_strict=True`` → raises
      :class:`DeploymentModeError` (the lifespan should let this
      propagate so K8s readinessProbe fails the pod).
    * On violations + ``cfg.deployment_mode_strict=False`` → logs a
      ``warning`` per violation. The caller is responsible for
      incrementing any monitoring gauge (e.g.
      ``gg_relay_partial_multiworker_config``) — kept out of this
      module so tests don't need Prometheus context.
    * Returns the violation list either way so callers can react
      (e.g. set a Prometheus gauge to ``len(violations)``).
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

    # If either backend selects redis, redis_url MUST be configured.
    # The strict_backend flag (Plan 8 v2.1) handles the unavailability
    # case; this check catches the simpler "selected redis but didn't
    # set the URL" misconfiguration before lifespan tries to connect.
    if (event_bus == "redis" or rate_limit == "redis") and not cfg.redis_url:
        violations.append(
            "redis_url is unset but at least one backend selects "
            "'redis'. Set RELAY_REDIS_URL=redis://... (or "
            "rediss:// for TLS in production)."
        )

    if violations and cfg.deployment_mode_strict:
        joined = "\n  - ".join(violations)
        raise DeploymentModeError(
            "Multi-worker deployment configuration violations "
            f"(strict mode):\n  - {joined}\n"
            "See docs/cluster.md for the recommended values."
        )
    if violations:
        for v in violations:
            logger.warning("multi_worker_config_violation: %s", v)

    return violations
