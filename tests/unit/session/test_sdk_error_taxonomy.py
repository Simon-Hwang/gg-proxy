"""SDKError taxonomy + classify_sdk_error tests — Plan 7 D7.25 / Task 14.

Six categories, each mapped to a stable HTTP status:

* ``connect``    → 503
* ``query``      → 400
* ``permission`` → 403
* ``transport``  → 502
* ``timeout``    → 504
* ``unknown``    → 500

Classification keys off the exception class name + lowercased message
so the buckets stay robust as the SDK reshuffles its hierarchy.
"""
from __future__ import annotations

import pytest

from gg_relay.core import (
    SDKConnectError,
    SDKError,
    SDKPermissionError,
    SDKQueryError,
    SDKTimeoutError,
    SDKTransportError,
    SDKUnknownError,
    classify_sdk_error,
)


class TestClassifySDKError:
    def test_classify_timeout_asyncio(self) -> None:
        """``TimeoutError`` (alias of ``asyncio.TimeoutError``) → :class:`SDKTimeoutError`."""
        err = classify_sdk_error(TimeoutError("slow"))
        assert isinstance(err, SDKTimeoutError)
        assert err.category == "timeout"
        assert err.http_status == 504

    def test_classify_timeout_via_message(self) -> None:
        """Class name doesn't say 'timeout' but message does."""
        err = classify_sdk_error(RuntimeError("operation timed out after 30s"))
        assert isinstance(err, SDKTimeoutError)
        assert err.category == "timeout"

    def test_classify_connect_error(self) -> None:
        """:class:`ConnectionError` → :class:`SDKConnectError` (503)."""
        err = classify_sdk_error(ConnectionError("connection refused"))
        assert isinstance(err, SDKConnectError)
        assert err.category == "connect"
        assert err.http_status == 503

    def test_classify_connect_via_message(self) -> None:
        err = classify_sdk_error(RuntimeError("upstream unreachable"))
        assert isinstance(err, SDKConnectError)

    def test_classify_permission_via_status_code(self) -> None:
        """403 / 401 in message → :class:`SDKPermissionError`."""
        err = classify_sdk_error(RuntimeError("HTTP 403 Forbidden"))
        assert isinstance(err, SDKPermissionError)
        assert err.category == "permission"
        assert err.http_status == 403

    def test_classify_permission_via_unauthorized(self) -> None:
        err = classify_sdk_error(RuntimeError("Unauthorized: missing API key"))
        assert isinstance(err, SDKPermissionError)

    def test_classify_transport_via_protocol_keyword(self) -> None:
        """Class or message containing 'protocol' / 'transport' / 'handshake'."""
        class TransportFailure(Exception):
            pass

        err = classify_sdk_error(TransportFailure("framing corrupted"))
        assert isinstance(err, SDKTransportError)
        assert err.http_status == 502

    def test_classify_query_invalid(self) -> None:
        """'invalid' in message → :class:`SDKQueryError` (400)."""
        err = classify_sdk_error(ValueError("invalid prompt: empty"))
        assert isinstance(err, SDKQueryError)
        assert err.category == "query"
        assert err.http_status == 400

    def test_classify_unknown_fallback(self) -> None:
        """Anything that doesn't match a bucket falls into ``unknown``."""
        err = classify_sdk_error(RuntimeError("freak thing happened"))
        assert isinstance(err, SDKUnknownError)
        assert err.category == "unknown"
        assert err.http_status == 500

    def test_classify_passthrough_idempotent(self) -> None:
        """Already-typed :class:`SDKError` instances pass through unchanged."""
        original = SDKTimeoutError("slow", original=None)
        result = classify_sdk_error(original)
        assert result is original
        # And calling again is still a no-op.
        assert classify_sdk_error(result) is original

    def test_original_exception_preserved(self) -> None:
        """The wrapped exception is exposed via ``.original``."""
        raw = ConnectionError("refused")
        err = classify_sdk_error(raw)
        assert err.original is raw


class TestSDKErrorBase:
    def test_base_class_is_exception(self) -> None:
        """All subclasses inherit from :class:`SDKError` and :class:`Exception`."""
        for cls in (
            SDKConnectError,
            SDKQueryError,
            SDKPermissionError,
            SDKTransportError,
            SDKTimeoutError,
            SDKUnknownError,
        ):
            assert issubclass(cls, SDKError)
            assert issubclass(cls, Exception)

    def test_categories_are_distinct(self) -> None:
        """No two subclasses share a ``category`` label."""
        categories = {
            cls.category
            for cls in (
                SDKConnectError,
                SDKQueryError,
                SDKPermissionError,
                SDKTransportError,
                SDKTimeoutError,
                SDKUnknownError,
            )
        }
        assert len(categories) == 6

    def test_http_status_codes_are_valid(self) -> None:
        """All subclasses map to 4xx or 5xx HTTP status."""
        for cls in (
            SDKConnectError,
            SDKQueryError,
            SDKPermissionError,
            SDKTransportError,
            SDKTimeoutError,
            SDKUnknownError,
        ):
            assert 400 <= cls.http_status <= 599

    def test_default_original_is_none(self) -> None:
        err = SDKError("boom")
        assert err.original is None

    def test_str_repr_preserves_message(self) -> None:
        err = SDKConnectError("connection refused", original=None)
        assert "connection refused" in str(err)


class TestRaiseAndCatch:
    def test_can_be_raised_and_caught_by_base(self) -> None:
        """Catching :class:`SDKError` catches every subclass."""
        for cls in (
            SDKConnectError,
            SDKQueryError,
            SDKPermissionError,
            SDKTransportError,
            SDKTimeoutError,
            SDKUnknownError,
        ):
            with pytest.raises(SDKError):
                raise cls("x")

    def test_subclass_specific_catch(self) -> None:
        """Specific subclasses can be filtered narrowly."""
        with pytest.raises(SDKTimeoutError):
            raise SDKTimeoutError("slow")
