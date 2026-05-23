"""Plan 7 Task 11 (D7.15) — SecretStr + sensitive-pattern masking.

Covers :func:`_mask_value` and :func:`redaction_processor`:

* :class:`pydantic.SecretStr` values are always masked.
* Strings matching :data:`SENSITIVE_PATTERN` (``api_key=…``,
  ``password=…``, ``token=…``, ``bearer X``) are masked.
* Non-sensitive primitives pass through unchanged.
* The structlog processor masks the whole event_dict, top level.
* The processor is wired in :func:`gg_relay.api.main._configure_structlog_redaction`.
"""
from __future__ import annotations

import inspect

from pydantic import SecretStr

from gg_relay.api.main import _configure_structlog_redaction
from gg_relay.redaction import redaction_processor
from gg_relay.redaction.engine import (
    SENSITIVE_PATTERN,
    _mask_value,
)
from gg_relay.redaction.engine import (
    redaction_processor as engine_redaction_processor,
)


class TestMaskValue:
    def test_secretstr_is_masked(self) -> None:
        assert _mask_value(SecretStr("abc")) == "***"

    def test_sensitive_string_api_key_equals(self) -> None:
        assert _mask_value("api_key=xyz123") == "***"

    def test_sensitive_string_password_colon(self) -> None:
        assert _mask_value("password: hunter2") == "***"

    def test_sensitive_string_bearer_token(self) -> None:
        assert _mask_value("Bearer eyJabc.def.ghi") == "***"

    def test_sensitive_string_token_assignment(self) -> None:
        assert _mask_value("token=ghp_AaAa") == "***"

    def test_normal_string_unchanged(self) -> None:
        assert _mask_value("hello world") == "hello world"

    def test_non_string_primitives_pass_through(self) -> None:
        assert _mask_value(42) == 42
        assert _mask_value(None) is None
        assert _mask_value(True) is True
        assert _mask_value(3.14) == 3.14

    def test_dict_passes_through_unchanged(self) -> None:
        d = {"foo": "bar"}
        assert _mask_value(d) is d

    def test_sensitive_pattern_compiled_case_insensitive(self) -> None:
        assert SENSITIVE_PATTERN.search("API_KEY=abc") is not None
        assert SENSITIVE_PATTERN.search("Password=abc") is not None


class TestRedactionProcessor:
    def test_engine_module_export_matches_package_export(self) -> None:
        """``redaction.engine.redaction_processor`` and
        ``redaction.redaction_processor`` resolve to the same callable."""
        assert redaction_processor is engine_redaction_processor

    def test_masks_secretstr_value_in_event_dict(self) -> None:
        out = redaction_processor(
            object(), "info", {"msg": "boot", "key": SecretStr("xyz")}
        )
        assert out["msg"] == "boot"
        assert out["key"] == "***"

    def test_masks_sensitive_pattern_in_event_dict(self) -> None:
        out = redaction_processor(
            object(),
            "warning",
            {"detail": "outgoing api_key=zzz to upstream"},
        )
        assert out["detail"] == "***"

    def test_passes_non_sensitive_through(self) -> None:
        out = redaction_processor(
            object(), "info", {"user_id": 17, "path": "/api/v1/sessions"}
        )
        assert out == {"user_id": 17, "path": "/api/v1/sessions"}

    def test_processor_signature_matches_structlog_protocol(self) -> None:
        """structlog processors are ``(logger, method_name, event_dict)``;
        our signature MUST match that arity so ``structlog.configure``
        accepts it without runtime adaptation."""
        sig = inspect.signature(redaction_processor)
        assert list(sig.parameters)[:3] == ["logger", "method_name", "event_dict"]


def test_structlog_processor_wired_in_app() -> None:
    """:func:`_configure_structlog_redaction` MUST install
    :func:`redaction_processor` as the first processor in the
    structlog pipeline. We invoke the configuration helper directly
    and inspect the resulting global config; this is the same path
    the lifespan takes at app startup.
    """
    import structlog

    _configure_structlog_redaction()
    cfg = structlog.get_config()
    processors = cfg.get("processors", [])
    assert processors, "structlog has no processors registered"
    assert processors[0] is redaction_processor, (
        "redaction_processor must be the FIRST structlog processor so "
        "downstream renderers never see plaintext secrets"
    )
