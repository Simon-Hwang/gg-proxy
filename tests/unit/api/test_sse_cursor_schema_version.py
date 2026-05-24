"""Plan 9 D9.9 — SSE durable-cursor parsing.

Single cursor format: ``Last-Event-ID: <events.seq>:<event_id>``.
The legacy v1 microsecond + v2 prefix-tagged formats that v1.4
LOCKED carried for rolling-deploy safety were removed at v0.9.0
pre-prod simplification. Only the row-seq path is recognised.
"""
from __future__ import annotations

from starlette.requests import Request

from gg_relay.api.sse import _parse_durable_last_event_id


def _req_with_header(value: str | None) -> Request:
    headers_list: list[tuple[bytes, bytes]] = []
    if value is not None:
        headers_list.append((b"last-event-id", value.encode("utf-8")))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/sessions/x/events",
        "headers": headers_list,
    }
    return Request(scope)


class TestDurableCursorParsing:
    """Single durable format: ``<seq>:<event_id>``."""

    def test_cursor_returns_int_seq(self) -> None:
        seq = _parse_durable_last_event_id(
            _req_with_header("42:abc-uuid-1234")
        )
        assert seq == 42

    def test_cursor_without_uuid_suffix_is_frame_cursor(self) -> None:
        """A bare integer is the per-session frame-cursor, NOT a
        durable cursor — return None so the SSE generator falls
        through to :func:`_parse_last_event_id`."""
        seq = _parse_durable_last_event_id(_req_with_header("42"))
        assert seq is None

    def test_cursor_with_garbage_seq_returns_none(self) -> None:
        seq = _parse_durable_last_event_id(
            _req_with_header("not-a-number:abc")
        )
        assert seq is None


class TestEdgeCases:
    """Negative tests — none of these should raise."""

    def test_missing_header_returns_none(self) -> None:
        assert _parse_durable_last_event_id(_req_with_header(None)) is None

    def test_legacy_frame_cursor_seq_prefix_returns_none(self) -> None:
        """``seq:42`` is handled by :func:`_parse_last_event_id` (the
        per-session frame cursor), not the durable parser."""
        assert _parse_durable_last_event_id(_req_with_header("seq:42")) is None

    def test_garbage_value_returns_none(self) -> None:
        assert _parse_durable_last_event_id(_req_with_header("garbage")) is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_durable_last_event_id(_req_with_header("")) is None
