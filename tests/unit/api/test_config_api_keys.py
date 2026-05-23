"""Plan 7 Task 6b / D7.26 — :class:`Config` API key surface back-compat.

Two properties live side-by-side after Task 6b:

* :attr:`Config.api_keys_with_labels` (new) — ``dict[key, label]`` for
  :class:`APIKeyAuthMiddleware` wiring.
* :attr:`Config.api_keys` (legacy) — ``set[str]`` keys-only view for
  callers that only need a configured-or-not check (CLI
  ``check-secrets``, ``status``, etc.).

The legacy view is deliberately kept so the CLI's ``status`` /
``check-secrets`` paths don't have to learn the labelled dict shape.
"""
from __future__ import annotations

from gg_relay.config import Config, missing_required


def test_api_keys_with_labels_returns_dict() -> None:
    cfg = Config()  # type: ignore[call-arg]
    cfg.api_keys_raw = "k1:alice,bob=k2,bare"
    out = cfg.api_keys_with_labels
    assert isinstance(out, dict)
    assert out["k1"] == "alice"
    assert out["k2"] == "bob"
    assert out["bare"].startswith("key-")


def test_api_keys_legacy_set_view_returns_set_of_keys() -> None:
    """``api_keys`` is a ``set[str]`` of the keys only — labels are
    intentionally dropped so callers that only care "is some key
    configured?" can keep doing ``if not cfg.api_keys:``."""
    cfg = Config()  # type: ignore[call-arg]
    cfg.api_keys_raw = "k1:alice,bob=k2,bare"
    out = cfg.api_keys
    assert isinstance(out, set)
    assert out == {"k1", "k2", "bare"}


def test_api_keys_legacy_set_view_empty() -> None:
    cfg = Config()  # type: ignore[call-arg]
    cfg.api_keys_raw = ""
    assert cfg.api_keys == set()
    assert cfg.api_keys_with_labels == {}


def test_check_secrets_path_uses_set_view() -> None:
    """``missing_required(cfg)`` reads ``cfg.api_keys`` via
    :func:`getattr`; the helper treats an empty set as missing so
    ``check-secrets`` still flags an empty ``RELAY_API_KEYS_RAW``."""
    cfg = Config()  # type: ignore[call-arg]
    cfg.api_keys_raw = ""
    cfg.public_base_url = "https://relay.example.com"
    from pydantic import SecretStr

    cfg.dashboard_admin_password = SecretStr("pw")
    cfg.dashboard_session_secret = SecretStr("ss")
    missing = missing_required(cfg)
    assert "api_keys" in missing

    cfg.api_keys_raw = "k1:alice"
    assert "api_keys" not in missing_required(cfg)
