"""Snapshot tests for :class:`FeishuCardBuilder` — Plan 6 Task 7.

These tests cover the *shape* of the Feishu interactive-card JSON
without touching HTTP. Drift in the schema (renamed tag, missing
action button, lost session_id) breaks immediately because we assert
on the exact dict structure.
"""
from __future__ import annotations

from gg_relay.core import HITLRequested, SessionCompleted, SessionStateChanged
from gg_relay.im.backends.feishu import FeishuCardBuilder
from gg_relay.im.card import RenderedCard


class TestHITLCard:
    def test_card_has_two_action_buttons(self):
        builder = FeishuCardBuilder()
        event = HITLRequested(
            session_id="s1",
            req_id="s1:r0",
            tool="WriteFile",
            args_redacted={"path": "/etc/passwd"},
        )
        rendered = builder.build_hitl_card(event, callback_base="https://x.test")
        assert isinstance(rendered, RenderedCard)
        elements = rendered.payload["elements"]
        action_block = next(e for e in elements if e["tag"] == "action")
        buttons = action_block["actions"]
        assert len(buttons) == 2
        decisions = sorted(b["value"]["decision"] for b in buttons)
        assert decisions == ["accept", "deny"]
        for b in buttons:
            assert b["value"]["session_id"] == "s1"
            assert b["value"]["req_id"] == "s1:r0"
        # Header still names the tool.
        assert rendered.payload["header"]["title"]["content"].endswith(
            "WriteFile"
        )
        # Card metadata picks Feishu's interactive msg_type.
        assert rendered.metadata == {"msg_type": "interactive"}
        # CardAction surface mirrors the JSON buttons for audit logging.
        assert len(rendered.actions) == 2
        styles = sorted(a.style for a in rendered.actions)
        assert styles == ["danger", "primary"]

    def test_callback_base_not_required_for_card_render(self):
        """Plan 6 D6.7=C — the builder accepts callback_base for
        forward compatibility but doesn't embed it (button values
        carry session+req_id inline). We just verify it doesn't crash
        with an empty string."""
        builder = FeishuCardBuilder()
        event = HITLRequested(
            session_id="s2",
            req_id="s2:r1",
            tool="bash",
            args_redacted={"cmd": "ls"},
        )
        rendered = builder.build_hitl_card(event, callback_base="")
        assert rendered.payload["header"]["template"] == "yellow"


class TestSessionEndCard:
    def test_status_appears_in_text(self):
        builder = FeishuCardBuilder()
        rendered = builder.build_session_end_card(
            SessionCompleted(session_id="sid-1", status="failed")
        )
        assert rendered.payload["text"].startswith("[failed]")
        assert "sid-1" in rendered.payload["text"]
        assert rendered.metadata == {"msg_type": "text"}

    def test_cost_appended_when_nonzero(self):
        builder = FeishuCardBuilder()
        rendered = builder.build_session_end_card(
            SessionCompleted(
                session_id="sid-1", status="completed", cost_usd=0.0042
            )
        )
        assert "$0.0042" in rendered.payload["text"]


class TestSessionStateCard:
    def test_running_to_paused_message(self):
        builder = FeishuCardBuilder()
        rendered = builder.build_session_state_card(
            SessionStateChanged(
                session_id="sX",
                from_state="running",
                to_state="paused",
                reason="hitl_wait",
            )
        )
        text = rendered.payload["text"]
        assert "sX" in text
        assert "paused" in text
        assert "running" in text
        assert "hitl_wait" in text
        assert rendered.metadata == {"msg_type": "text"}

    def test_no_reason_when_absent(self):
        builder = FeishuCardBuilder()
        rendered = builder.build_session_state_card(
            SessionStateChanged(
                session_id="sX", from_state="paused", to_state="running"
            )
        )
        # No trailing parens for the optional reason.
        assert "()" not in rendered.payload["text"]


class TestBuildOther:
    def test_returns_none(self):
        from gg_relay.core import Heartbeat

        builder = FeishuCardBuilder()
        assert builder.build_other(Heartbeat()) is None
