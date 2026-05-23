"""Alert-on-complete end-to-end (D8.23) — Plan 8 Task 11.

D8.23 wires the ``complete`` rule list so a session tagged ``notify``
fires an alert at terminal-completed time. This test exercises the
real Plan 8 components stitched together (FailureSubscriber +
AlertRouter + FeishuCardBuilder) without spinning a FastAPI app or
a live bus task — direct invocation keeps the wiring contract under
test even when the bus / IM transport contracts evolve.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from gg_relay.core import SessionStateChanged
from gg_relay.im.backends.feishu import FeishuCardBuilder
from gg_relay.im.card import RenderedCard
from gg_relay.subscribers.alert_router import AlertRouter
from gg_relay.subscribers.failure_subscriber import FailureSubscriber

pytestmark = pytest.mark.asyncio


@dataclass
class _SpyBackend:
    sent: list[RenderedCard] = field(default_factory=list)

    async def send_card(self, card: RenderedCard) -> None:
        self.sent.append(card)


@dataclass
class _SeededStore:
    """Tiny store stub returning a single canned row keyed by sid.

    Real :class:`gg_relay.store.repository.SessionRepository` returns
    a :class:`sqlalchemy.engine.RowMapping`; the duck-typed dict here
    is enough for the subscriber's ``row.get("owner")`` /
    ``row.get("tags")`` calls."""

    rows: dict[str, dict[str, Any]]

    async def get_session(self, sid: str) -> dict[str, Any] | None:
        return self.rows.get(sid)


async def test_complete_tag_notify_triggers_alert() -> None:
    """Session tagged ``notify`` + complete rule ``tag=notify`` →
    alert card lands in the spy backend with the right header /
    @mention plumbing."""
    backend = _SpyBackend()
    builder = FeishuCardBuilder()
    router = AlertRouter(
        rules={"complete": ["tag=notify"]},
        feishu_user_mapping={"alice": "ou_alice_xyz"},
        backend=backend,
        card_builder=builder,
        default_channel="oc-team",
    )
    store = _SeededStore(
        rows={
            "sid-complete-notify": {
                "owner": "alice",
                "tags": ["notify"],
            }
        }
    )
    sub = FailureSubscriber(
        bus=None,  # type: ignore[arg-type]
        alert_router=router,
        store=store,
    )

    event = SessionStateChanged(
        session_id="sid-complete-notify",
        from_state="running",
        to_state="completed",
        reason=None,
    )

    dispatched = await sub.handle(event)
    assert dispatched is True
    assert len(backend.sent) == 1
    card = backend.sent[0]
    assert card.channel_id == "oc-team"
    assert card.metadata["msg_type"] == "interactive"
    assert card.metadata["alert_event_type"] == "session_completed"
    assert card.metadata["mention_open_id"] == "ou_alice_xyz"
    header = card.payload["header"]
    assert "completed" in header["title"]["content"].lower()
    assert header["template"] == "green"
    body = next(
        el["content"]
        for el in card.payload["elements"]
        if el.get("tag") == "markdown"
    )
    assert "sid-complete-notify" in body
    assert "alice" in body
    assert "ou_alice_xyz" in body  # <at id="ou_alice_xyz">


async def test_complete_untagged_does_not_alert() -> None:
    """Completion without the ``notify`` tag falls outside the
    default complete rule → no alert is sent."""
    backend = _SpyBackend()
    builder = FeishuCardBuilder()
    router = AlertRouter(backend=backend, card_builder=builder)
    store = _SeededStore(
        rows={"sid-quiet": {"owner": "alice", "tags": []}}
    )
    sub = FailureSubscriber(
        bus=None,  # type: ignore[arg-type]
        alert_router=router,
        store=store,
    )

    event = SessionStateChanged(
        session_id="sid-quiet",
        from_state="running",
        to_state="completed",
        reason=None,
    )

    dispatched = await sub.handle(event)
    assert dispatched is False
    assert backend.sent == []
