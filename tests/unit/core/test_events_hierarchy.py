"""RelayEvent hierarchy + frame → event dispatch table tests (Plan 5 Task 1).

D5.11=B: 11 concrete subclasses; D5.3: delivery_tier defaults reflect
subscriber-queue policy (lossy events drop on backpressure, durable
events block).
"""
from __future__ import annotations

import dataclasses
import json
from typing import get_args
from uuid import UUID

import pytest

from gg_relay.core.events import (
    _FRAME_TO_EVENT,
    Heartbeat,
    HITLRequested,
    HITLResolved,
    InstallDone,
    InstallError,
    RelayEvent,
    RelayEventT,
    SessionCompleted,
    SessionCreated,
    SessionOutputChunk,
    SessionStateChanged,
    ToolRequested,
    ToolResolved,
    frame_to_event,
)

ALL_SUBCLASSES = (
    SessionCreated,
    SessionStateChanged,
    SessionOutputChunk,
    SessionCompleted,
    HITLRequested,
    HITLResolved,
    ToolRequested,
    ToolResolved,
    InstallDone,
    InstallError,
    Heartbeat,
)


class TestSubclassShape:
    """Each concrete subclass is frozen + slots + has sensible defaults."""

    @pytest.mark.parametrize("cls", ALL_SUBCLASSES, ids=[c.__name__ for c in ALL_SUBCLASSES])
    def test_frozen(self, cls):
        inst = cls()
        with pytest.raises(dataclasses.FrozenInstanceError):
            inst.session_id = "mutated"  # type: ignore[misc]

    @pytest.mark.parametrize("cls", ALL_SUBCLASSES, ids=[c.__name__ for c in ALL_SUBCLASSES])
    def test_slots(self, cls):
        inst = cls()
        # ``__slots__`` is on the class; concrete instances have no ``__dict__``.
        assert not hasattr(inst, "__dict__")

    @pytest.mark.parametrize("cls", ALL_SUBCLASSES, ids=[c.__name__ for c in ALL_SUBCLASSES])
    def test_event_id_uuid(self, cls):
        assert isinstance(cls().event_id, UUID)

    @pytest.mark.parametrize("cls", ALL_SUBCLASSES, ids=[c.__name__ for c in ALL_SUBCLASSES])
    def test_occurred_at_tz_aware(self, cls):
        assert cls().occurred_at.tzinfo is not None


class TestDeliveryTierDefaults:
    """D5.3 semantics: durable for control events, lossy for telemetry."""

    @pytest.mark.parametrize(
        "cls",
        [
            SessionCreated,
            SessionStateChanged,
            SessionCompleted,
            HITLRequested,
            HITLResolved,
            ToolRequested,
            ToolResolved,
            InstallError,
        ],
        ids=lambda c: c.__name__,
    )
    def test_durable_default(self, cls):
        assert cls().delivery_tier == "durable"

    @pytest.mark.parametrize(
        "cls",
        [SessionOutputChunk, InstallDone, Heartbeat],
        ids=lambda c: c.__name__,
    )
    def test_lossy_default(self, cls):
        assert cls().delivery_tier == "lossy"


class TestUnion:
    def test_relay_event_t_covers_all_subclasses(self):
        union_members = set(get_args(RelayEventT))
        assert union_members == set(ALL_SUBCLASSES)

    def test_base_relay_event_not_in_union(self):
        assert RelayEvent not in set(get_args(RelayEventT))


class TestEventIdUnique:
    def test_event_id_unique_across_100_instances(self):
        ids = {SessionOutputChunk().event_id for _ in range(100)}
        assert len(ids) == 100


class TestJsonSerializable:
    def test_asdict_json_dumps_roundtrip(self):
        ev = SessionCompleted(
            session_id="sid-1",
            status="completed",
            tokens={"in": 10, "out": 20},
            cost_usd=0.0125,
        )
        payload = dataclasses.asdict(ev)
        text = json.dumps(payload, default=str)
        again = json.loads(text)
        assert again["session_id"] == "sid-1"
        assert again["status"] == "completed"
        assert again["tokens"] == {"in": 10, "out": 20}
        assert again["delivery_tier"] == "durable"
        assert "event_id" in again
        assert "occurred_at" in again


class TestFrameToEvent:
    """`_FRAME_TO_EVENT` covers every wire-level frame type and produces
    the correctly-typed dataclass with redacted payloads."""

    def test_msg_chunk_to_session_output_chunk(self):
        frame = {"type": "msg.chunk", "seq": 5, "data": {"text": "hi"}}
        ev = frame_to_event("sid", frame)
        assert isinstance(ev, SessionOutputChunk)
        assert ev.session_id == "sid"
        assert ev.seq == 5
        assert ev.payload["data"] == {"text": "hi"}
        assert ev.delivery_tier == "lossy"

    def test_tool_request_to_tool_requested(self):
        frame = {
            "type": "tool.request",
            "seq": 1,
            "req_id": "r-1",
            "tool": "Write",
            "args": {"path": "/tmp/x"},
        }
        ev = frame_to_event("sid", frame)
        assert isinstance(ev, ToolRequested)
        assert ev.req_id == "r-1"
        assert ev.tool == "Write"
        assert ev.args_redacted == {"path": "/tmp/x"}
        assert ev.delivery_tier == "durable"

    def test_tool_result_to_tool_resolved(self):
        frame = {
            "type": "tool.result",
            "seq": 2,
            "req_id": "r-1",
            "ok": True,
            "result": {"bytes_written": 4},
        }
        ev = frame_to_event("sid", frame)
        assert isinstance(ev, ToolResolved)
        assert ev.ok is True
        assert ev.result_redacted == {"bytes_written": 4}

    def test_install_done(self):
        frame = {
            "type": "install.done",
            "profile_id": "minimal",
            "modules": ["a", "b"],
            "duration_ms": 1234,
        }
        ev = frame_to_event("sid", frame)
        assert isinstance(ev, InstallDone)
        assert ev.profile_id == "minimal"
        assert ev.modules == ("a", "b")
        assert ev.duration_ms == 1234

    def test_install_error_distinct_from_runtime_error(self):
        installer = frame_to_event(
            "sid",
            {"type": "install.error", "code": "boom", "message": "no install.sh"},
        )
        runtime = frame_to_event(
            "sid",
            {"type": "error", "code": "boom2", "message": "runner died"},
        )
        assert isinstance(installer, InstallError)
        assert isinstance(runtime, InstallError)
        assert installer.code == "boom"
        assert runtime.code == "boom2"

    def test_session_end_normalises_unknown_status(self):
        for raw, expected in [
            ("completed", "completed"),
            ("cancelled", "cancelled"),
            ("crashed", "failed"),
            ("failed", "failed"),
            ("nonsense", "completed"),
        ]:
            ev = frame_to_event(
                "sid",
                {"type": "session.end", "status": raw, "tokens": {"a": "5"}},
            )
            assert isinstance(ev, SessionCompleted)
            assert ev.status == expected
            assert ev.tokens == {"a": 5}

    def test_pong_to_heartbeat(self):
        ev = frame_to_event("sid", {"type": "pong", "runtime_id": "abc"})
        assert isinstance(ev, Heartbeat)
        assert ev.runtime_id == "abc"
        assert ev.delivery_tier == "lossy"

    def test_unknown_type_returns_none(self):
        assert frame_to_event("sid", {"type": "future.type", "payload": {}}) is None

    def test_missing_type_returns_none(self):
        assert frame_to_event("sid", {"payload": {}}) is None

    def test_dispatch_table_covers_all_wire_frame_types(self):
        # The wire-level frame variants in transport/protocol.py are 8: the
        # 7 in _FRAME_TO_EVENT *plus* "install.error" / "error" handled by
        # the same factory. This test enforces that any time someone adds
        # a new wire frame the table grows accordingly.
        expected = {
            "msg.chunk",
            "tool.request",
            "tool.result",
            "install.done",
            "install.error",
            "error",
            "session.end",
            "pong",
        }
        assert set(_FRAME_TO_EVENT) == expected


class TestPublisherDefaults:
    """Sanity check publisher-initialised subclasses (manager-emitted)."""

    def test_session_created_durable(self):
        assert SessionCreated(session_id="s").delivery_tier == "durable"

    def test_session_state_changed_durable(self):
        assert (
            SessionStateChanged(
                session_id="s", from_state="queued", to_state="running"
            ).delivery_tier
            == "durable"
        )
