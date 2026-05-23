"""Plan 7 Task 11 (D7.14) — secrets fail-fast validation.

:meth:`Config.validate_required_secrets` is the lifespan boot guard:

* Dev mode (``production_mode=False``) — missing API keys produces a
  warning, lifespan continues. ``allow_no_keys=True`` silences the
  warning for unit-test sandboxes.
* Production mode — raises :class:`RuntimeError` on:

    1. Missing API keys (empty ``RELAY_API_KEYS_RAW``).
    2. Half-configured Feishu (any one of app id / app secret /
       webhook secret set without the rest).
    3. Database URL still equal to the sqlite dev default.
"""
from __future__ import annotations

import logging

import pytest
from pydantic import SecretStr

from gg_relay.config import DEFAULT_SQLITE_URL, Config


class TestDevMode:
    def test_dev_mode_missing_keys_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Dev mode + missing API keys → ``logger.warning`` + no raise."""
        cfg = Config()  # type: ignore[call-arg]
        cfg.production_mode = False
        cfg.api_keys_raw = ""
        cfg.allow_no_keys = False
        with caplog.at_level(logging.WARNING, logger="gg_relay.config"):
            cfg.validate_required_secrets()
        warnings = [
            rec for rec in caplog.records
            if "no API keys configured" in rec.getMessage()
        ]
        assert warnings, (
            "dev mode should emit a 'no API keys' warning when "
            "RELAY_API_KEYS_RAW is empty"
        )

    def test_dev_mode_allow_no_keys_silences_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = Config()  # type: ignore[call-arg]
        cfg.production_mode = False
        cfg.api_keys_raw = ""
        cfg.allow_no_keys = True
        with caplog.at_level(logging.WARNING, logger="gg_relay.config"):
            cfg.validate_required_secrets()
        assert not [
            rec for rec in caplog.records
            if "no API keys configured" in rec.getMessage()
        ]


class TestProductionMode:
    def test_production_mode_missing_keys_raises(self) -> None:
        cfg = Config()  # type: ignore[call-arg]
        cfg.production_mode = True
        cfg.api_keys_raw = ""
        # Avoid tripping the Postgres-URL check too — we only want
        # the API-key path to surface in this assertion.
        cfg.database_url = "postgresql+asyncpg://u:p@localhost/db"
        with pytest.raises(RuntimeError) as exc_info:
            cfg.validate_required_secrets()
        assert "RELAY_API_KEYS_RAW required" in str(exc_info.value)

    def test_production_mode_feishu_partial_raises(self) -> None:
        """Configuring just ``feishu_app_id`` (and nothing else) MUST
        raise — half-configured Feishu silently 401s every callback."""
        cfg = Config()  # type: ignore[call-arg]
        cfg.production_mode = True
        cfg.api_keys_raw = "k1"
        cfg.database_url = "postgresql+asyncpg://u:p@localhost/db"
        cfg.feishu_app_id = SecretStr("cli_app_id")
        # Intentionally leaves feishu_app_secret + webhook_secret unset.
        with pytest.raises(RuntimeError) as exc_info:
            cfg.validate_required_secrets()
        msg = str(exc_info.value)
        assert "feishu_app_secret" in msg
        assert "feishu_webhook_secret" in msg

    def test_production_mode_sqlite_default_raises(self) -> None:
        cfg = Config()  # type: ignore[call-arg]
        cfg.production_mode = True
        cfg.api_keys_raw = "k1"
        # Sqlite dev default — must be rejected in production.
        cfg.database_url = DEFAULT_SQLITE_URL
        with pytest.raises(RuntimeError) as exc_info:
            cfg.validate_required_secrets()
        assert "Postgres" in str(exc_info.value) or (
            "RELAY_DATABASE_URL" in str(exc_info.value)
        )

    def test_production_mode_all_good_returns_none(self) -> None:
        """Healthy production config returns ``None`` (no raise)."""
        cfg = Config()  # type: ignore[call-arg]
        cfg.production_mode = True
        cfg.api_keys_raw = "k1:alice"
        cfg.database_url = "postgresql+asyncpg://u:p@localhost/db"
        cfg.feishu_app_id = SecretStr("cli_app_id")
        cfg.feishu_app_secret = SecretStr("cli_app_secret")
        cfg.feishu_webhook_secret = SecretStr("whk-secret")
        assert cfg.validate_required_secrets() is None

    def test_production_mode_reports_all_problems(self) -> None:
        """All problems reported in a single error so operators can fix
        the env in one pass."""
        cfg = Config()  # type: ignore[call-arg]
        cfg.production_mode = True
        cfg.api_keys_raw = ""
        cfg.database_url = DEFAULT_SQLITE_URL
        cfg.feishu_app_id = SecretStr("cli_app_id")  # forces Feishu check
        with pytest.raises(RuntimeError) as exc_info:
            cfg.validate_required_secrets()
        msg = str(exc_info.value)
        assert "RELAY_API_KEYS_RAW" in msg
        assert "feishu" in msg.lower()
        assert "Postgres" in msg or "RELAY_DATABASE_URL" in msg
