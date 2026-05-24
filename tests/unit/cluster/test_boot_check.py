"""Plan 9 v0.9.0-rc D9.11 — multi-worker boot-time check tests.

Validates the data-driven check defuses the silent multi-worker
failure mode (replicas > 1 + inmemory backends) without bricking
single-worker deployments.

The check has four observable behaviours, one test each:

1. ``single_worker`` mode → always returns ``[]``, no warnings.
2. ``multi_worker`` + safe backends + redis_url → returns ``[]``.
3. ``multi_worker`` + unsafe backends → returns violations (default
   warn-only), or raises (when ``deployment_mode_strict=True``).
4. ``MULTI_WORKER_SAFE_BACKENDS`` is data-driven (set, not hardcoded)
   so future Kafka/NATS backends can be added without modifying the
   check function.
"""
from __future__ import annotations

import logging

import pytest

from gg_relay.cluster import (
    MULTI_WORKER_SAFE_BACKENDS,
    DeploymentModeError,
    validate_deployment_mode,
)
from gg_relay.config import Config


def _make_config(**overrides) -> Config:
    """Build a minimal Config with single-worker defaults + overrides."""
    base = {
        "database_url": "sqlite+aiosqlite:///:memory:",
        "deployment_mode": "single_worker",
        "deployment_mode_strict": False,
        "event_bus_backend": "inmemory",
        "rate_limit_backend": "inmemory",
        "redis_url": None,
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


class TestSingleWorkerMode:
    """single_worker mode skips validation entirely (back-compat)."""

    def test_inmemory_backends_single_worker_no_violations(self) -> None:
        cfg = _make_config()
        assert validate_deployment_mode(cfg) == []

    def test_redis_backends_single_worker_no_violations(self) -> None:
        """Even valid multi-worker config doesn't trigger in single mode."""
        cfg = _make_config(
            event_bus_backend="redis",
            rate_limit_backend="redis",
            redis_url="redis://localhost:6379/0",
        )
        assert validate_deployment_mode(cfg) == []


class TestMultiWorkerSafeConfig:
    """multi_worker mode + cluster-safe backends → no violations."""

    def test_redis_both_backends_no_violations(self) -> None:
        cfg = _make_config(
            deployment_mode="multi_worker",
            event_bus_backend="redis",
            rate_limit_backend="redis",
            redis_url="redis://localhost:6379/0",
        )
        assert validate_deployment_mode(cfg) == []

    def test_rediss_tls_url_accepted(self) -> None:
        """Plan 9.1 D9.1 recommends rediss:// (TLS); the boot check
        doesn't enforce this — D9.1 handles the TLS preference — but
        it MUST not reject rediss:// either."""
        cfg = _make_config(
            deployment_mode="multi_worker",
            event_bus_backend="redis",
            rate_limit_backend="redis",
            redis_url="rediss://prod.cluster:6379/0",
        )
        assert validate_deployment_mode(cfg) == []


class TestMultiWorkerUnsafeConfig:
    """multi_worker mode + inmemory backends → violations."""

    def test_inmemory_event_bus_reported(self) -> None:
        cfg = _make_config(
            deployment_mode="multi_worker",
            event_bus_backend="inmemory",
            rate_limit_backend="redis",
            redis_url="redis://localhost:6379/0",
        )
        violations = validate_deployment_mode(cfg)
        assert len(violations) == 1
        assert "event_bus_backend" in violations[0]

    def test_inmemory_rate_limit_reported(self) -> None:
        cfg = _make_config(
            deployment_mode="multi_worker",
            event_bus_backend="redis",
            rate_limit_backend="inmemory",
            redis_url="redis://localhost:6379/0",
        )
        violations = validate_deployment_mode(cfg)
        assert len(violations) == 1
        assert "rate_limit_backend" in violations[0]

    def test_missing_redis_url_reported_when_backend_selects_redis(
        self,
    ) -> None:
        cfg = _make_config(
            deployment_mode="multi_worker",
            event_bus_backend="redis",
            rate_limit_backend="redis",
            redis_url=None,
        )
        violations = validate_deployment_mode(cfg)
        assert len(violations) == 1
        assert "redis_url" in violations[0]

    def test_warn_only_default(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """deployment_mode_strict=False (default) → log warnings, return list."""
        cfg = _make_config(
            deployment_mode="multi_worker",
            event_bus_backend="inmemory",
            rate_limit_backend="inmemory",
        )
        with caplog.at_level(logging.WARNING, logger="gg_relay.cluster"):
            violations = validate_deployment_mode(cfg)
        assert len(violations) >= 1
        # Each violation should have triggered a log line.
        warning_msgs = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            "multi_worker_config_violation" in m for m in warning_msgs
        )


class TestStrictMode:
    """deployment_mode_strict=True → fail-fast on any violation."""

    def test_raises_deployment_mode_error(self) -> None:
        cfg = _make_config(
            deployment_mode="multi_worker",
            deployment_mode_strict=True,
            event_bus_backend="inmemory",
        )
        with pytest.raises(DeploymentModeError) as exc_info:
            validate_deployment_mode(cfg)
        assert "Multi-worker" in str(exc_info.value)
        assert "event_bus_backend" in str(exc_info.value)

    def test_strict_mode_passes_clean_config(self) -> None:
        cfg = _make_config(
            deployment_mode="multi_worker",
            deployment_mode_strict=True,
            event_bus_backend="redis",
            rate_limit_backend="redis",
            redis_url="redis://localhost:6379/0",
        )
        # No raise.
        assert validate_deployment_mode(cfg) == []


class TestDataDrivenBackendSet:
    """MULTI_WORKER_SAFE_BACKENDS must be extensible (Reviewer D
    Round 2 MAJOR — future plans add Kafka/NATS without code change)."""

    def test_safe_backends_is_set_type(self) -> None:
        # frozenset/set — supports membership and len()
        assert isinstance(MULTI_WORKER_SAFE_BACKENDS, frozenset)

    def test_safe_backends_contains_redis(self) -> None:
        assert "redis" in MULTI_WORKER_SAFE_BACKENDS

    def test_safe_backends_does_not_contain_inmemory(self) -> None:
        """Sanity: inmemory MUST be the silent-failure case the
        check defuses, NOT a multi-worker-safe value."""
        assert "inmemory" not in MULTI_WORKER_SAFE_BACKENDS
