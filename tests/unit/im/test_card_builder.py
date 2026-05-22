"""Tests for the CardBuilder Protocol — Plan 6 Task 5 / D6.7=C.

These tests verify the *shape* of the Protocol (method signatures,
default ``build_other``, ``RenderedCard`` / ``CardAction`` immutability)
without depending on any concrete platform implementation. The Feishu
adapter has its own snapshot tests in ``test_feishu_card_builder.py``
(Plan 6 Task 7).
"""
from __future__ import annotations

from typing import Any

import pytest

from gg_relay.core import (
    Heartbeat,
    HITLRequested,
    RelayEvent,
    SessionCompleted,
    SessionStateChanged,
)
from gg_relay.im.card import CardAction, CardBuilder, RenderedCard


class _StubBuilder:
    """Minimal builder used in this module — overrides only the three
    required methods so we can verify the Protocol's structural typing
    works without subclassing CardBuilder (which is a Protocol, not a
    base class). Returns a deterministic payload identifying the source
    event so assertions can check method dispatch."""

    def build_hitl_card(
        self, event: HITLRequested, *, callback_base: str
    ) -> RenderedCard:
        return RenderedCard(
            payload={
                "kind": "hitl",
                "session_id": event.session_id,
                "req_id": event.req_id,
                "tool": event.tool,
                "args": dict(event.args_redacted),
                "callback_base": callback_base,
            },
            actions=(
                CardAction(label="Approve", payload={"d": "a"}, style="primary"),
                CardAction(label="Deny", payload={"d": "d"}, style="danger"),
            ),
        )

    def build_session_end_card(self, event: SessionCompleted) -> RenderedCard:
        return RenderedCard(
            payload={
                "kind": "end",
                "session_id": event.session_id,
                "status": event.status,
            },
        )

    def build_session_state_card(
        self, event: SessionStateChanged
    ) -> RenderedCard:
        return RenderedCard(
            payload={
                "kind": "state",
                "session_id": event.session_id,
                "to_state": event.to_state,
            },
            channel_id=None if event.to_state == "running" else "ops-alerts",
        )

    def build_other(self, event: RelayEvent) -> RenderedCard | None:
        # Explicitly opt out of rendering everything else. The Protocol's
        # default `return None` would have the same effect for callers
        # that go through the Protocol type, but @runtime_checkable
        # isinstance() requires the method physically exists on the
        # class — Protocol default bodies are NOT inherited by
        # structural-only implementers.
        del event
        return None


class TestProtocolDispatch:
    def test_hitl_dispatch(self):
        builder: CardBuilder = _StubBuilder()
        event = HITLRequested(
            session_id="s1",
            req_id="r1",
            tool="bash",
            args_redacted={"cmd": "ls"},
        )
        card = builder.build_hitl_card(event, callback_base="https://x")
        assert card.payload["kind"] == "hitl"
        assert card.payload["tool"] == "bash"
        assert card.payload["callback_base"] == "https://x"
        assert card.payload["args"] == {"cmd": "ls"}
        # Actions are immutable tuples — ensure we got two.
        assert len(card.actions) == 2
        assert card.actions[0].style == "primary"
        assert card.actions[1].style == "danger"

    def test_session_end_dispatch(self):
        builder: CardBuilder = _StubBuilder()
        event = SessionCompleted(session_id="s2", status="failed")
        card = builder.build_session_end_card(event)
        assert card.payload["kind"] == "end"
        assert card.payload["status"] == "failed"
        # No actions / channel by default.
        assert card.actions == ()
        assert card.channel_id is None

    def test_session_state_dispatch_routes_channel(self):
        builder: CardBuilder = _StubBuilder()
        running = builder.build_session_state_card(
            SessionStateChanged(
                session_id="s3", from_state="paused", to_state="running"
            )
        )
        # to_state=running → builder leaves channel_id unset (default).
        assert running.channel_id is None
        paused = builder.build_session_state_card(
            SessionStateChanged(
                session_id="s3", from_state="running", to_state="paused"
            )
        )
        # Any non-running state routes to ops-alerts in the stub.
        assert paused.channel_id == "ops-alerts"


class TestBuildOtherDefault:
    def test_returns_none_for_unknown_event_type(self):
        """The Protocol's default ``build_other`` returns None so the
        subscriber can short-circuit dispatch for unhandled events
        without raising. Builders typically delegate to the Protocol's
        default via ``return None``."""
        builder = _StubBuilder()
        # Heartbeat is a real RelayEvent subclass not covered by any
        # required method — exercises the default escape hatch.
        assert builder.build_other(Heartbeat()) is None

    def test_protocol_default_is_no_op(self):
        """Even calling the Protocol method directly produces None,
        documenting the no-op contract."""

        class _DefaultsOnly:
            def build_hitl_card(
                self, event: HITLRequested, *, callback_base: str
            ) -> RenderedCard:
                del event, callback_base
                return RenderedCard(payload={})

            def build_session_end_card(
                self, event: SessionCompleted
            ) -> RenderedCard:
                del event
                return RenderedCard(payload={})

            def build_session_state_card(
                self, event: SessionStateChanged
            ) -> RenderedCard:
                del event
                return RenderedCard(payload={})

        # Bind the Protocol's default to a structural impl explicitly.
        assert CardBuilder.build_other(_DefaultsOnly(), Heartbeat()) is None


class TestRenderedCardImmutability:
    def test_rendered_card_is_frozen(self):
        card = RenderedCard(payload={"x": 1})
        with pytest.raises((AttributeError, TypeError)):
            card.payload = {"x": 2}  # type: ignore[misc]

    def test_card_action_is_frozen(self):
        action = CardAction(label="ok", payload={})
        with pytest.raises((AttributeError, TypeError)):
            action.label = "no"  # type: ignore[misc]


class TestRuntimeCheckable:
    def test_stub_is_recognised_as_card_builder(self):
        """``@runtime_checkable`` lets ``isinstance(x, CardBuilder)``
        verify structural conformance for plug-in registries."""
        assert isinstance(_StubBuilder(), CardBuilder)

    def test_non_builder_is_rejected(self):
        class _NotABuilder:
            def unrelated(self) -> Any:
                return None

        assert not isinstance(_NotABuilder(), CardBuilder)


class TestRenderedCardEquality:
    def test_equal_when_fields_equal(self):
        a = RenderedCard(payload={"x": 1}, channel_id="c", actions=())
        b = RenderedCard(payload={"x": 1}, channel_id="c", actions=())
        assert a == b

    def test_unequal_when_payload_differs(self):
        a = RenderedCard(payload={"x": 1})
        b = RenderedCard(payload={"x": 2})
        assert a != b


def _unused_to_silence_ruff(*_: RelayEvent) -> None:
    """Keep the RelayEvent import alive — referenced as a type in
    _StubBuilder's signatures but not directly used in assertions."""
