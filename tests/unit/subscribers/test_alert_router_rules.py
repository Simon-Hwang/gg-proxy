"""AlertRouter rule + cooldown + mention tests — Plan 8 Task 11 (D8.7).

The router exposes three concerns that the unit tests cover separately:

1. ``_matches`` — the small condition DSL (always / end_reason literal /
   tag=name) — tested without any backend so failures point straight
   at the rule logic.
2. ``_cooldown_check`` — exercised end-to-end through ``dispatch`` so
   the LRU + monotonic-clock interaction stays under test.
3. ``resolve_mention`` — pure dict lookup, included here so the same
   suite proves the ``cfg.feishu_user_mapping`` → ``open_id`` plumbing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from gg_relay.im.card import RenderedCard
from gg_relay.subscribers.alert_router import AlertRouter

# Module-level asyncio mark would warn on the sync ``_matches`` tests
# below; we annotate the async classes individually instead.


@dataclass
class _RecordingBackend:
    sent: list[RenderedCard] = field(default_factory=list)

    async def send_card(self, card: RenderedCard) -> None:
        self.sent.append(card)


@dataclass
class _RecordingBuilder:
    """Card builder stub that captures the kwargs handed to
    ``build_alert_card`` so each test can assert on the resolved
    mention id without parsing a real Feishu card payload."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def build_alert_card(self, **kwargs: Any) -> RenderedCard:
        self.calls.append(kwargs)
        return RenderedCard(
            payload={
                "kind": "alert",
                "event_type": kwargs["event_type"],
                "mention_open_id": kwargs.get("mention_open_id") or "",
            },
            metadata={"msg_type": "interactive"},
        )


def _router(
    *,
    rules: dict[str, list[str]] | None = None,
    mapping: dict[str, str] | None = None,
    cooldown_s: int = 300,
    default_channel: str | None = None,
) -> tuple[AlertRouter, _RecordingBackend, _RecordingBuilder]:
    backend = _RecordingBackend()
    builder = _RecordingBuilder()
    router = AlertRouter(
        rules=rules,
        feishu_user_mapping=mapping,
        backend=backend,
        card_builder=builder,
        default_channel=default_channel,
        cooldown_s=cooldown_s,
    )
    return router, backend, builder


class TestRuleMatching:
    def test_rule_always_matches_failed(self) -> None:
        router, _, _ = _router(rules={"fail": ["always"]})
        assert (
            router._matches(
                event_type="session_failed",
                end_reason="anything",
                tags=[],
            )
            is True
        )

    def test_rule_specific_end_reason(self) -> None:
        router, _, _ = _router(rules={"cancel": ["timeout_recovered"]})
        assert (
            router._matches(
                event_type="session_cancelled",
                end_reason="timeout_recovered",
                tags=[],
            )
            is True
        )
        assert (
            router._matches(
                event_type="session_cancelled",
                end_reason="other",
                tags=[],
            )
            is False
        )

    def test_rule_tag_filter(self) -> None:
        router, _, _ = _router(rules={"complete": ["tag=notify"]})
        assert (
            router._matches(
                event_type="session_completed",
                end_reason="unknown",
                tags=["notify"],
            )
            is True
        )
        assert (
            router._matches(
                event_type="session_completed",
                end_reason="unknown",
                tags=["other"],
            )
            is False
        )

    def test_unknown_event_type_never_matches(self) -> None:
        """Defensive: an upstream typo (``"session_oops"``) MUST NOT
        flip the always-on fail rule into a permissive match."""
        router, _, _ = _router(rules={"fail": ["always"]})
        assert (
            router._matches(
                event_type="session_oops",
                end_reason="boom",
                tags=[],
            )
            is False
        )

    def test_defaults_cover_fail_cancel_timeout_complete_notify(self) -> None:
        router, _, _ = _router()
        assert (
            router._matches(
                event_type="session_failed",
                end_reason="anything",
                tags=[],
            )
            is True
        )
        assert (
            router._matches(
                event_type="session_cancelled",
                end_reason="timeout",
                tags=[],
            )
            is True
        )
        assert (
            router._matches(
                event_type="session_completed",
                end_reason="unknown",
                tags=["notify"],
            )
            is True
        )
        # Bare completed (no notify tag) → no default alert.
        assert (
            router._matches(
                event_type="session_completed",
                end_reason="unknown",
                tags=[],
            )
            is False
        )


@pytest.mark.asyncio
class TestCooldown:
    async def test_cooldown_prevents_repeat(self) -> None:
        router, backend, builder = _router(
            rules={"fail": ["always"]}, cooldown_s=300
        )
        sent = await router.dispatch(
            event_type="session_failed",
            session_id="sid",
            owner="alice",
            tags=[],
            end_reason="http:502",
            event=object(),
        )
        assert sent is True
        assert len(backend.sent) == 1

        again = await router.dispatch(
            event_type="session_failed",
            session_id="sid",
            owner="alice",
            tags=[],
            end_reason="http:502",
            event=object(),
        )
        assert again is False
        assert len(backend.sent) == 1
        assert len(builder.calls) == 1

    async def test_cooldown_distinguishes_owner_and_reason(self) -> None:
        router, backend, _ = _router(
            rules={"fail": ["always"]}, cooldown_s=300
        )
        await router.dispatch(
            event_type="session_failed",
            session_id="s1",
            owner="alice",
            tags=[],
            end_reason="http:502",
            event=object(),
        )
        await router.dispatch(
            event_type="session_failed",
            session_id="s2",
            owner="bob",
            tags=[],
            end_reason="http:502",
            event=object(),
        )
        await router.dispatch(
            event_type="session_failed",
            session_id="s3",
            owner="alice",
            tags=[],
            end_reason="timeout",
            event=object(),
        )
        # Three distinct tuples → three sends.
        assert len(backend.sent) == 3

    async def test_cooldown_expiry_allows_resend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Past the cooldown window the same key alerts again. We
        fast-forward the monotonic clock rather than sleeping so the
        test stays sub-millisecond."""
        clock = {"now": 1000.0}

        def _fake_monotonic() -> float:
            return clock["now"]

        monkeypatch.setattr(
            "gg_relay.subscribers.alert_router.time.monotonic",
            _fake_monotonic,
        )
        router, backend, _ = _router(
            rules={"fail": ["always"]}, cooldown_s=60
        )
        await router.dispatch(
            event_type="session_failed",
            session_id="s",
            owner="o",
            tags=[],
            end_reason="r",
            event=object(),
        )
        clock["now"] += 61.0  # walk past the cooldown
        await router.dispatch(
            event_type="session_failed",
            session_id="s",
            owner="o",
            tags=[],
            end_reason="r",
            event=object(),
        )
        assert len(backend.sent) == 2


@pytest.mark.asyncio
class TestMentionResolve:
    async def test_mention_resolved_from_feishu_user_mapping(self) -> None:
        router, _, builder = _router(
            rules={"fail": ["always"]},
            mapping={"alice": "ou_alice_open_id"},
        )
        sent = await router.dispatch(
            event_type="session_failed",
            session_id="s",
            owner="alice",
            tags=[],
            end_reason="r",
            event=object(),
        )
        assert sent is True
        assert builder.calls[0]["mention_open_id"] == "ou_alice_open_id"

    async def test_unknown_owner_falls_through_with_none_mention(
        self,
    ) -> None:
        router, _, builder = _router(
            rules={"fail": ["always"]},
            mapping={"alice": "ou_alice"},
        )
        await router.dispatch(
            event_type="session_failed",
            session_id="s",
            owner="bob",
            tags=[],
            end_reason="r",
            event=object(),
        )
        assert builder.calls[0]["mention_open_id"] is None


@pytest.mark.asyncio
class TestDispatchEdgeCases:
    async def test_missing_backend_logs_and_returns_false(self) -> None:
        router = AlertRouter(
            rules={"fail": ["always"]},
            backend=None,
            card_builder=None,
        )
        sent = await router.dispatch(
            event_type="session_failed",
            session_id="s",
            owner=None,
            tags=[],
            end_reason="r",
            event=object(),
        )
        assert sent is False

    async def test_backend_send_failure_does_not_raise(self) -> None:
        @dataclass
        class _RaisingBackend:
            async def send_card(self, card: RenderedCard) -> None:
                del card
                raise RuntimeError("Feishu 502")

        builder = _RecordingBuilder()
        router = AlertRouter(
            rules={"fail": ["always"]},
            backend=_RaisingBackend(),
            card_builder=builder,
        )
        sent = await router.dispatch(
            event_type="session_failed",
            session_id="s",
            owner=None,
            tags=[],
            end_reason="r",
            event=object(),
        )
        assert sent is False

    async def test_default_channel_applied_when_card_has_none(self) -> None:
        router, backend, _ = _router(
            rules={"fail": ["always"]},
            default_channel="oc-alerts",
        )
        await router.dispatch(
            event_type="session_failed",
            session_id="s",
            owner=None,
            tags=[],
            end_reason="r",
            event=object(),
        )
        assert backend.sent[0].channel_id == "oc-alerts"


@pytest.mark.asyncio
class TestLRUCap:
    async def test_lru_eviction_caps_memory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Distinct owners blow past the LRU cap; the oldest entry
        gets evicted but recent entries stay in the cooldown set."""
        monkeypatch.setattr(AlertRouter, "LRU_CAP", 4)
        clock = {"now": time.monotonic()}
        monkeypatch.setattr(
            "gg_relay.subscribers.alert_router.time.monotonic",
            lambda: clock["now"],
        )
        router, _, _ = _router(
            rules={"fail": ["always"]}, cooldown_s=300
        )
        for i in range(6):
            await router.dispatch(
                event_type="session_failed",
                session_id="s",
                owner=f"u{i}",
                tags=[],
                end_reason="r",
                event=object(),
            )
        assert len(router._last_alert) == 4
        # First two owners should have been evicted.
        assert ("session_failed", "u0", "r") not in router._last_alert
        assert ("session_failed", "u5", "r") in router._last_alert
