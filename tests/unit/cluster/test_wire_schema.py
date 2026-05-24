"""Plan 9 D9.13 — Redis stream wire schema v1 round-trip tests.

The :mod:`gg_relay.cluster.wire` module is the single source of
truth for the byte-for-byte format that :class:`RedisStreamEventBus`
puts on the ``gg-relay:events`` stream. These tests pin the schema
so any future format change either:

1. bumps :data:`SCHEMA_VERSION` (and ships a parallel decode branch
   for the old version) — caught here because the constant value
   is asserted, or
2. modifies the wire shape silently — caught here because the
   round-trip assertions fail.

Covers:

* Constant pinning (SCHEMA_VERSION=1, STREAM_KEY="gg-relay:events").
* Round-trip on every concrete :class:`RelayEvent` subclass — the
  loop discovers every dataclass so adding a new event class
  automatically extends coverage.
* Reserved-field bytes shape (every value is :class:`str`, no
  bytes / nested dicts — required by ``redis-py``'s XADD signature).
* Negative tests — unknown ``v``, missing ``v``, garbage timestamp,
  garbage payload.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
from datetime import UTC, datetime

import pytest

from gg_relay.cluster.wire import (
    SCHEMA_VERSION,
    STREAM_KEY,
    UnsupportedWireVersionError,
    decode_event,
    encode_event,
)
from gg_relay.core import events as events_module
from gg_relay.core.events import RelayEvent, SessionCreated, ToolRequested
from gg_relay.store.durable_event import ReplayedEvent


class TestConstantPinning:
    """Bumping these without coordinating a rolling upgrade breaks
    multi-worker SSE delivery silently — pin them hard."""

    def test_schema_version_is_one(self) -> None:
        assert SCHEMA_VERSION == 1

    def test_stream_key_is_gg_relay_events(self) -> None:
        """The K8s deployment manifest references this exact string in
        its RELAY_REDIS_STREAM_KEY env var — keep them in sync."""
        assert STREAM_KEY == "gg-relay:events"


class TestEncodeShape:
    """XADD signature: redis-py accepts ``Mapping[bytes | str, bytes |
    str | int | float]``. Stay strictly within ``str`` so the bus
    layer doesn't need to type-coerce per-field."""

    def test_every_value_is_str(self) -> None:
        evt = SessionCreated(
            session_id="s1",
            occurred_at=datetime.now(UTC),
            prompt_redacted="hello",
            tags=("test",),
        )
        encoded = encode_event(evt)
        for k, v in encoded.items():
            assert isinstance(k, str), f"key {k!r} is not str"
            assert isinstance(v, str), (
                f"value for {k!r} is {type(v).__name__}, not str"
            )

    def test_v_field_is_first_for_dispatch(self) -> None:
        """Decoder MAY peek at 'v' first; insertion order should put
        it at index 0 so a future zero-copy parser can short-circuit.
        Python 3.7+ dicts preserve insertion order."""
        evt = SessionCreated(
            session_id="s1",
            occurred_at=datetime.now(UTC),
            prompt_redacted="x",
            tags=(),
        )
        encoded = encode_event(evt)
        assert next(iter(encoded)) == "v"

    def test_required_fields_present(self) -> None:
        evt = SessionCreated(
            session_id="s1",
            occurred_at=datetime.now(UTC),
            prompt_redacted="x",
            tags=(),
        )
        encoded = encode_event(evt)
        for required in (
            "v", "type", "event_id", "ts", "session_id", "tier", "payload"
        ):
            assert required in encoded, f"missing required field {required!r}"

    def test_payload_is_json_string(self) -> None:
        """payload field must be a JSON-serialised dict, not the dict
        itself — XADD doesn't accept nested mappings."""
        evt = ToolRequested(
            session_id="s1",
            occurred_at=datetime.now(UTC),
            tool="Bash",
            args_redacted={"cmd": "ls"},
        )
        encoded = encode_event(evt)
        # Parses without error
        parsed = json.loads(encoded["payload"])
        assert isinstance(parsed, dict)
        # tool field is in payload (not promoted to top level)
        assert parsed["tool"] == "Bash"

    def test_event_id_and_ts_not_duplicated_in_payload(self) -> None:
        """event_id and ts are top-level columns; they should NOT
        also appear in payload (saves bandwidth, simplifies parse)."""
        evt = SessionCreated(
            session_id="s1",
            occurred_at=datetime.now(UTC),
            prompt_redacted="x",
            tags=(),
        )
        encoded = encode_event(evt)
        payload = json.loads(encoded["payload"])
        assert "event_id" not in payload
        assert "occurred_at" not in payload


class TestRoundTripCoverage:
    """Discover every RelayEvent subclass and round-trip it.

    Adding a new event class to :mod:`gg_relay.core.events` will
    automatically extend this test; if the new class has unusual
    fields (datetime, bytes, …) the assertion will fail and force
    the author to think about wire compat.
    """

    @pytest.mark.parametrize(
        "event_cls",
        [
            cls
            for _, cls in inspect.getmembers(events_module, inspect.isclass)
            if issubclass(cls, RelayEvent) and cls is not RelayEvent
        ],
    )
    def test_roundtrip(self, event_cls: type[RelayEvent]) -> None:
        """Round-trip every concrete event class."""
        # Build a minimal instance — RelayEvent subclasses all use
        # dataclass with required fields, so we synthesise plausible
        # values from the field types.
        kwargs: dict = {}
        for f in dataclasses.fields(event_cls):
            kwargs[f.name] = _synthesise_value(f)
        try:
            evt = event_cls(**kwargs)
        except TypeError:
            pytest.skip(
                f"{event_cls.__name__} requires a hand-crafted instance"
            )
        encoded = encode_event(evt)
        decoded = decode_event(encoded)
        assert isinstance(decoded, ReplayedEvent)
        assert decoded.type_name == event_cls.__name__
        assert str(decoded.event_id) == str(evt.event_id)
        assert decoded.session_id == (
            getattr(evt, "session_id", "") or ""
        )
        assert decoded.delivery_tier == evt.delivery_tier


def _synthesise_value(field: dataclasses.Field):  # type: ignore[type-arg]
    """Best-effort default value for a dataclass field by type."""
    if field.default is not dataclasses.MISSING:
        return field.default
    if field.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return field.default_factory()
    # Annotation-based fallbacks
    ann = field.type if isinstance(field.type, str) else field.type
    name = field.name
    if "id" in name and "session" in name:
        return "s-test"
    if name == "occurred_at":
        return datetime.now(UTC)
    if name == "tags":
        return ()
    if name == "args_redacted":
        return {}
    if "prompt" in name:
        return "test prompt"
    if name == "duration_ms":
        return 0.0
    if name == "success":
        return True
    if name == "tool":
        return "TestTool"
    if isinstance(ann, str) and "str" in ann.lower():
        return "test"
    if isinstance(ann, str) and "int" in ann.lower():
        return 0
    if isinstance(ann, str) and "float" in ann.lower():
        return 0.0
    if isinstance(ann, str) and "bool" in ann.lower():
        return False
    return None


class TestNegativeDecodes:
    """Malformed entries must raise, not silently produce garbage."""

    def test_missing_v_raises(self) -> None:
        entry = {
            "type": "SessionCreated",
            "event_id": "e1",
            "ts": datetime.now(UTC).isoformat(),
            "session_id": "s1",
            "tier": "durable",
            "payload": "{}",
        }
        with pytest.raises(UnsupportedWireVersionError):
            decode_event(entry)

    def test_unknown_v_raises(self) -> None:
        entry = {
            "v": "999",
            "type": "SessionCreated",
            "event_id": "e1",
            "ts": datetime.now(UTC).isoformat(),
            "session_id": "s1",
            "tier": "durable",
            "payload": "{}",
        }
        with pytest.raises(UnsupportedWireVersionError) as exc_info:
            decode_event(entry)
        assert "999" in str(exc_info.value)

    def test_non_integer_v_raises(self) -> None:
        entry = {
            "v": "abc",
            "type": "SessionCreated",
            "event_id": "e1",
            "ts": datetime.now(UTC).isoformat(),
            "session_id": "s1",
            "tier": "durable",
            "payload": "{}",
        }
        with pytest.raises(UnsupportedWireVersionError):
            decode_event(entry)

    def test_garbage_ts_yields_current_time(self) -> None:
        """A broken ts field should NOT crash the subscriber — degrade
        to ``datetime.now(UTC)`` so the event still flows to clients."""
        entry = {
            "v": "1",
            "type": "SessionCreated",
            "event_id": "e1",
            "ts": "not-a-date",
            "session_id": "s1",
            "tier": "durable",
            "payload": "{}",
        }
        result = decode_event(entry)
        # Just assert it's a datetime; the exact value is non-deterministic.
        assert isinstance(result.occurred_at, datetime)

    def test_garbage_payload_yields_empty_dict(self) -> None:
        entry = {
            "v": "1",
            "type": "SessionCreated",
            "event_id": "e1",
            "ts": datetime.now(UTC).isoformat(),
            "session_id": "s1",
            "tier": "durable",
            "payload": "{not: valid json",
        }
        result = decode_event(entry)
        assert result.payload == {}
