"""Plan 7 Task 15 (D7.23) — OTEL endpoint env-var alias tests.

``Config.otel_endpoint`` accepts both the relay's own
``RELAY_OTEL_ENDPOINT`` and the upstream OTel convention
``OTEL_EXPORTER_OTLP_ENDPOINT``. When both are present the relay-prefixed
variant must win so operators migrating from an OTel-native deployment
get a predictable transition step.
"""
from __future__ import annotations

import pytest

from gg_relay.config import Config


@pytest.fixture(autouse=True)
def _clear_otel_env(monkeypatch: pytest.MonkeyPatch):
    # Ensure prior test pollution / shell exports don't leak into the
    # alias resolution under test.
    monkeypatch.delenv("RELAY_OTEL_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    yield


class TestOtelEndpointAlias:
    def test_relay_otel_endpoint_read(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.chdir(tmp_path)  # avoid picking up repo .env
        monkeypatch.setenv("RELAY_OTEL_ENDPOINT", "http://relay-only:4318")
        cfg = Config()  # type: ignore[call-arg]
        assert cfg.otel_endpoint == "http://relay-only:4318"

    def test_otel_exporter_otlp_endpoint_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-native:4318"
        )
        cfg = Config()  # type: ignore[call-arg]
        assert cfg.otel_endpoint == "http://otel-native:4318"

    def test_relay_otel_endpoint_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("RELAY_OTEL_ENDPOINT", "http://relay-wins:4318")
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://should-be-overridden:4318"
        )
        cfg = Config()  # type: ignore[call-arg]
        assert cfg.otel_endpoint == "http://relay-wins:4318"

    def test_neither_set_falls_back_to_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        cfg = Config()  # type: ignore[call-arg]
        assert cfg.otel_endpoint is None
