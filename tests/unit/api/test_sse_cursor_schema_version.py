"""Plan 9 v0.9.0-rc D9.9a — SSE cursor schema_version parsing.

The Plan 7 D7.17 ``Last-Event-ID: <microsecond-seq>:<event_id>`` v1
cursor and the new Plan 9 D9.9a ``Last-Event-ID: v2:<row-seq>:<event_id>``
v2 cursor must dispatch through the same ``_parse_durable_last_event_id``
function so the SSE router can pick the right replay path:

* v1 → ``EventBus.replay_after`` (microsecond-derived seq)
* v2 → ``EventBus.replay_after_seq`` (events.seq column, post-0012a)

Backward compatibility:

* v0.8.x clients reconnecting against v0.9.0+ servers send a v1
  cursor; the parser MUST keep walking the microsecond path.
* v0.9.0+ clients reconnecting send a v2 cursor; the parser MUST
  switch to the row-seq path.

This compat window is ≥ 2 minor releases (Plan 9 v1.4 §D9.9a).
"""
from __future__ import annotations

from starlette.requests import Request

from gg_relay.api.sse import _parse_durable_last_event_id


def _req_with_header(value: str | None) -> Request:
    """Build a minimal Request with just a ``Last-Event-ID`` header."""
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


class TestV1CursorParsing:
    """v0.8.x microsecond cursor: ``<microsecond-seq>:<uuid>``."""

    def test_v1_cursor_returns_schema_1_and_seq(self) -> None:
        cursor = _parse_durable_last_event_id(
            _req_with_header("1716540000123456:abc-uuid")
        )
        assert cursor == (1, 1716540000123456)

    def test_v1_cursor_with_only_seq_no_uuid_suffix(self) -> None:
        """Some legacy clients may have stripped the UUID — bare int
        without ``:`` is treated as a frame-cursor (returns None)."""
        cursor = _parse_durable_last_event_id(
            _req_with_header("1716540000123456")
        )
        # No ``:`` separator → returns None (caller falls through to
        # frame-cursor parser).
        assert cursor is None


class TestV2CursorParsing:
    """v0.9.0+ row-seq cursor: ``v2:<row-seq>:<uuid>``."""

    def test_v2_cursor_returns_schema_2_and_seq(self) -> None:
        cursor = _parse_durable_last_event_id(
            _req_with_header("v2:42:abc-uuid")
        )
        assert cursor == (2, 42)

    def test_v2_cursor_without_uuid_suffix(self) -> None:
        """``v2:<n>`` (no UUID) is still a valid v2 cursor — the
        SSE renderer always appends UUID, but a manually-crafted
        client cursor may omit it."""
        cursor = _parse_durable_last_event_id(_req_with_header("v2:42"))
        assert cursor == (2, 42)

    def test_v2_cursor_with_garbage_seq_returns_none(self) -> None:
        cursor = _parse_durable_last_event_id(
            _req_with_header("v2:not-a-number:abc")
        )
        assert cursor is None


class TestEdgeCases:
    """Negative tests — none of these should raise."""

    def test_missing_header_returns_none(self) -> None:
        cursor = _parse_durable_last_event_id(_req_with_header(None))
        assert cursor is None

    def test_legacy_frame_cursor_seq_prefix_returns_none(self) -> None:
        """``seq:42`` is handled by :func:`_parse_last_event_id` (the
        per-session frame cursor), not the durable parser."""
        cursor = _parse_durable_last_event_id(_req_with_header("seq:42"))
        assert cursor is None

    def test_garbage_value_returns_none(self) -> None:
        cursor = _parse_durable_last_event_id(_req_with_header("garbage"))
        assert cursor is None

    def test_empty_string_returns_none(self) -> None:
        cursor = _parse_durable_last_event_id(_req_with_header(""))
        assert cursor is None
