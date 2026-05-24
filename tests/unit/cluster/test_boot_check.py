"""Plan 9 D9.11 — multi-worker boot-time check tests.

Validates the data-driven check defuses the silent multi-worker
failure mode (replicas > 1 + inmemory backends) without bricking
single-worker deployments.

The check has three observable behaviours:

1. ``single_worker`` mode → always returns ``[]``, no warnings.
2. ``multi_worker`` + safe backends + redis_url → returns ``[]``.
3. ``multi_worker`` + unsafe backends → raises
   :class:`DeploymentModeError` (always strict; the warn-only escape
   hatch was removed at v0.9.0 pre-prod simplification).

Plus:
4. ``MULTI_WORKER_SAFE_BACKENDS`` is data-driven (frozenset, not
   hardcoded) so future Kafka/NATS backends can be added without
   modifying the check function.
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
        "event_bus_backend": "inmemory",
        "rate_limit_backend": "inmemory",
        "redis_url": None,
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


class TestSingleWorkerMode:
    """single_worker mode skips validation entirely."""

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
        """D9.1 recommends rediss:// (TLS); the boot check doesn't
        enforce this — D9.1 handles the TLS preference — but it
        MUST not reject rediss:// either."""
        cfg = _make_config(
            deployment_mode="multi_worker",
            event_bus_backend="redis",
            rate_limit_backend="redis",
            redis_url="rediss://prod.cluster:6379/0",
        )
        assert validate_deployment_mode(cfg) == []


class TestMultiWorkerUnsafeConfigRaises:
    """multi_worker mode + inmemory backends → DeploymentModeError."""

    def test_inmemory_event_bus_raises(self) -> None:
        cfg = _make_config(
            deployment_mode="multi_worker",
            event_bus_backend="inmemory",
            rate_limit_backend="redis",
            redis_url="redis://localhost:6379/0",
        )
        with pytest.raises(DeploymentModeError) as exc_info:
            validate_deployment_mode(cfg)
        assert "event_bus_backend" in str(exc_info.value)

    def test_inmemory_rate_limit_raises(self) -> None:
        cfg = _make_config(
            deployment_mode="multi_worker",
            event_bus_backend="redis",
            rate_limit_backend="inmemory",
            redis_url="redis://localhost:6379/0",
        )
        with pytest.raises(DeploymentModeError) as exc_info:
            validate_deployment_mode(cfg)
        assert "rate_limit_backend" in str(exc_info.value)

    def test_missing_redis_url_raises_when_backend_selects_redis(
        self,
    ) -> None:
        cfg = _make_config(
            deployment_mode="multi_worker",
            event_bus_backend="redis",
            rate_limit_backend="redis",
            redis_url=None,
        )
        with pytest.raises(DeploymentModeError) as exc_info:
            validate_deployment_mode(cfg)
        assert "redis_url" in str(exc_info.value)

    def test_violations_logged_at_error_level(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Pre-raise, every violation is logged at ERROR so operators
        running with a not-yet-tuned logger still see why the boot
        died."""
        cfg = _make_config(
            deployment_mode="multi_worker",
            event_bus_backend="inmemory",
            rate_limit_backend="inmemory",
        )
        with (
            caplog.at_level(logging.ERROR, logger="gg_relay.cluster"),
            pytest.raises(DeploymentModeError),
        ):
            validate_deployment_mode(cfg)
        error_msgs = [
            r.message for r in caplog.records if r.levelno == logging.ERROR
        ]
        assert any(
            "multi_worker_config_violation" in m for m in error_msgs
        )


class TestDataDrivenBackendSet:
    """MULTI_WORKER_SAFE_BACKENDS must be extensible (Reviewer D
    Round 2 MAJOR — future plans add Kafka/NATS without code change)."""

    def test_safe_backends_is_set_type(self) -> None:
        assert isinstance(MULTI_WORKER_SAFE_BACKENDS, frozenset)

    def test_safe_backends_contains_redis(self) -> None:
        assert "redis" in MULTI_WORKER_SAFE_BACKENDS

    def test_safe_backends_does_not_contain_inmemory(self) -> None:
        """Sanity: inmemory MUST be the silent-failure case the
        check defuses, NOT a multi-worker-safe value."""
        assert "inmemory" not in MULTI_WORKER_SAFE_BACKENDS
