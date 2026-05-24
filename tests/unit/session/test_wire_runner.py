"""Unit tests for the container entry-point ``wire_runner``.

The full end-to-end path needs a live unix socket + claude CLI and lives
behind ``@requires_docker @requires_api_key``; here we exercise the parts
that are pure CPU — env validation and the helper context manager.
"""
from __future__ import annotations

import pytest

from gg_relay.session.runner import wire_runner


def test_check_env_passes_when_all_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GG_RELAY_SPEC_JSON", '{"prompt": "hi"}')
    monkeypatch.setenv("GG_RELAY_SOCKET", "/var/run/gg-relay/x.sock")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    wire_runner._check_env(tcp_mode=False)  # must not raise


def test_check_env_raises_on_missing_spec(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GG_RELAY_SPEC_JSON", raising=False)
    monkeypatch.setenv("GG_RELAY_SOCKET", "/var/run/gg-relay/x.sock")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with pytest.raises(SystemExit, match="GG_RELAY_SPEC_JSON"):
        wire_runner._check_env(tcp_mode=False)


def test_check_env_raises_on_missing_socket(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GG_RELAY_SPEC_JSON", '{"prompt": "hi"}')
    monkeypatch.delenv("GG_RELAY_SOCKET", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with pytest.raises(SystemExit, match="GG_RELAY_SOCKET"):
        wire_runner._check_env(tcp_mode=False)


def test_check_env_raises_on_missing_api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GG_RELAY_SPEC_JSON", '{"prompt": "hi"}')
    monkeypatch.setenv("GG_RELAY_SOCKET", "/var/run/gg-relay/x.sock")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        wire_runner._check_env(tcp_mode=False)


def test_check_env_tcp_mode_passes(monkeypatch: pytest.MonkeyPatch):
    """Plan 9 D9.8 — tcp_mode requires the listener vars instead of
    GG_RELAY_SOCKET. GG_RELAY_SOCKET being absent must NOT fail."""
    monkeypatch.setenv("GG_RELAY_SPEC_JSON", '{"prompt": "hi"}')
    monkeypatch.delenv("GG_RELAY_SOCKET", raising=False)
    monkeypatch.setenv("GG_RELAY_TCP_LISTEN", "0.0.0.0:9001")
    monkeypatch.setenv("RELAY_RUNNER_AUTH_TOKEN", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    wire_runner._check_env(tcp_mode=True)


def test_check_env_tcp_mode_missing_token(monkeypatch: pytest.MonkeyPatch):
    """Missing RELAY_RUNNER_AUTH_TOKEN in TCP mode must fail-fast
    (otherwise the runner would bind a listener that auths anyone)."""
    monkeypatch.setenv("GG_RELAY_SPEC_JSON", '{"prompt": "hi"}')
    monkeypatch.setenv("GG_RELAY_TCP_LISTEN", "0.0.0.0:9001")
    monkeypatch.delenv("RELAY_RUNNER_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with pytest.raises(SystemExit, match="RELAY_RUNNER_AUTH_TOKEN"):
        wire_runner._check_env(tcp_mode=True)


def test_check_env_tcp_mode_missing_listen(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GG_RELAY_SPEC_JSON", '{"prompt": "hi"}')
    monkeypatch.delenv("GG_RELAY_TCP_LISTEN", raising=False)
    monkeypatch.setenv("RELAY_RUNNER_AUTH_TOKEN", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with pytest.raises(SystemExit, match="GG_RELAY_TCP_LISTEN"):
        wire_runner._check_env(tcp_mode=True)


def test_suppress_signal_install_error_swallows_value_error():
    cm = wire_runner._suppress_signal_install_error()
    with cm:
        raise ValueError("loop has no add_signal_handler on this platform")


def test_suppress_signal_install_error_swallows_not_implemented():
    cm = wire_runner._suppress_signal_install_error()
    with cm:
        raise NotImplementedError


def test_suppress_signal_install_error_propagates_other_exceptions():
    cm = wire_runner._suppress_signal_install_error()
    with pytest.raises(RuntimeError), cm:
        raise RuntimeError("not what we suppress")
