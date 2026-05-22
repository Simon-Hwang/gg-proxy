"""IMSubscriber tests — Plan 6 Task 6 / D6.8=A.

Verifies the bus → CardBuilder → IMBackend wiring without involving
HTTP or any real platform. Uses :class:`_StubBackend` to record every
``send_card`` call and :class:`_StubBuilder` to render deterministic
payloads.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

import pytest

from gg_relay.core import (
    EventBus,
    Heartbeat,
    HITLRequested,
    RelayEvent,
    SessionCompleted,
    SessionStateChanged,
)
from gg_relay.im.card import CardAction, RenderedCard
from gg_relay.im.subscriber import IMSubscriber

pytestmark = pytest.mark.asyncio


@dataclass
class _StubBuilder:
    """Builder that records every invocation and produces a
    deterministic card per event type."""

    hitl_calls: list[tuple[HITLRequested, str]] = field(default_factory=list)
    end_calls: list[SessionCompleted] = field(default_factory=list)
    state_calls: list[SessionStateChanged] = field(default_factory=list)
    raise_on_hitl: bool = False

    def build_hitl_card(
        self, event: HITLRequested, *, callback_base: str
    ) -> RenderedCard:
        self.hitl_calls.append((event, callback_base))
        if self.raise_on_hitl:
            raise RuntimeError("builder boom")
        return RenderedCard(
            payload={"kind": "hitl", "tool": event.tool},
            actions=(CardAction(label="Approve", payload={"d": "a"}),),
        )

    def build_session_end_card(self, event: SessionCompleted) -> RenderedCard:
        self.end_calls.append(event)
        return RenderedCard(payload={"kind": "end", "sid": event.session_id})

    def build_session_state_card(
        self, event: SessionStateChanged
    ) -> RenderedCard:
        self.state_calls.append(event)
        return RenderedCard(
            payload={"kind": "state", "to": event.to_state},
            channel_id=(
                # builder picks a channel ONLY for paused — exercises
                # the builder-supplied channel_id precedence rule
                "ops-alerts" if event.to_state == "paused" else None
            ),
        )

    def build_other(self, event: RelayEvent) -> RenderedCard | None:
        del event
        return None


@dataclass
class _StubBackend:
    name: str = "stub"
    sent: list[RenderedCard] = field(default_factory=list)
    raise_on_send: BaseException | None = None

    async def send_card(self, card: RenderedCard) -> None:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent.append(card)

    async def notify_hitl_pending(self, **_: object) -> None:
        return None

    async def notify_session_end(self, **_: object) -> None:
        return None


async def _start_subscriber(
    bus: EventBus,
    builder: _StubBuilder,
    backend: _StubBackend,
    *,
    default_channel: str | None = "default-chan",
    public_callback_base: str = "https://relay.test",
    channel_resolver: object = None,
) -> tuple[IMSubscriber, asyncio.Task[None]]:
    sub = IMSubscriber(
        bus=bus,
        builder=builder,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        default_channel=default_channel,
        public_callback_base=public_callback_base,
        channel_resolver=channel_resolver,  # type: ignore[arg-type]
    )
    task = asyncio.create_task(sub.run(), name="im-sub")
    # Yield so subscribe() registrations land before the first publish.
    await asyncio.sleep(0)
    return sub, task


async def _stop(sub: IMSubscriber, task: asyncio.Task[None]) -> None:
    await sub.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


# ── tests ─────────────────────────────────────────────────────────────


class TestEventDispatch:
    async def test_hitl_event_dispatched_with_callback_base(self):
        bus = EventBus()
        builder = _StubBuilder()
        backend = _StubBackend()
        sub, task = await _start_subscriber(
            bus, builder, backend, public_callback_base="https://x.test"
        )
        try:
            event = HITLRequested(
                session_id="s1",
                req_id="r1",
                tool="bash",
                args_redacted={"cmd": "ls"},
            )
            await bus.publish(event)
            await asyncio.sleep(0.05)
            assert builder.hitl_calls == [(event, "https://x.test")]
            assert len(backend.sent) == 1
            assert backend.sent[0].payload == {"kind": "hitl", "tool": "bash"}
        finally:
            await _stop(sub, task)

    async def test_session_completed_dispatched(self):
        bus = EventBus()
        builder = _StubBuilder()
        backend = _StubBackend()
        sub, task = await _start_subscriber(bus, builder, backend)
        try:
            event = SessionCompleted(session_id="s2", status="failed")
            await bus.publish(event)
            await asyncio.sleep(0.05)
            assert builder.end_calls == [event]
            assert backend.sent[0].payload["kind"] == "end"
        finally:
            await _stop(sub, task)

    async def test_session_state_dispatched(self):
        bus = EventBus()
        builder = _StubBuilder()
        backend = _StubBackend()
        sub, task = await _start_subscriber(bus, builder, backend)
        try:
            event = SessionStateChanged(
                session_id="s3", from_state="running", to_state="paused"
            )
            await bus.publish(event)
            await asyncio.sleep(0.05)
            assert builder.state_calls == [event]
            assert backend.sent[0].payload == {"kind": "state", "to": "paused"}
        finally:
            await _stop(sub, task)

    async def test_unrelated_event_ignored(self):
        """Heartbeats etc. should not produce a card — _DISPATCH only
        covers the three registered event classes."""
        bus = EventBus()
        builder = _StubBuilder()
        backend = _StubBackend()
        sub, task = await _start_subscriber(bus, builder, backend)
        try:
            await bus.publish(Heartbeat())
            await asyncio.sleep(0.05)
            assert backend.sent == []
        finally:
            await _stop(sub, task)


class TestChannelResolution:
    async def test_default_channel_when_card_has_none(self):
        bus = EventBus()
        builder = _StubBuilder()
        backend = _StubBackend()
        sub, task = await _start_subscriber(
            bus, builder, backend, default_channel="default-chan"
        )
        try:
            # SessionCompleted's builder picks channel_id=None →
            # fallback to default-chan.
            await bus.publish(
                SessionCompleted(session_id="s1", status="completed")
            )
            await asyncio.sleep(0.05)
            assert backend.sent[0].channel_id == "default-chan"
        finally:
            await _stop(sub, task)

    async def test_builder_channel_id_used_when_present(self):
        bus = EventBus()
        builder = _StubBuilder()
        backend = _StubBackend()
        sub, task = await _start_subscriber(
            bus, builder, backend, default_channel="default-chan"
        )
        try:
            # Builder sets channel_id='ops-alerts' for paused state.
            await bus.publish(
                SessionStateChanged(
                    session_id="s1", from_state="running", to_state="paused"
                )
            )
            await asyncio.sleep(0.05)
            assert backend.sent[0].channel_id == "ops-alerts"
        finally:
            await _stop(sub, task)

    async def test_channel_resolver_overrides_everything(self):
        bus = EventBus()
        builder = _StubBuilder()
        backend = _StubBackend()

        def _resolver(event: RelayEvent) -> str | None:
            return "tenant-billing" if isinstance(event, SessionCompleted) else None

        sub, task = await _start_subscriber(
            bus,
            builder,
            backend,
            default_channel="default-chan",
            channel_resolver=_resolver,
        )
        try:
            # SessionCompleted → resolver picks tenant-billing.
            await bus.publish(SessionCompleted(session_id="s1", status="completed"))
            await asyncio.sleep(0.05)
            assert backend.sent[0].channel_id == "tenant-billing"
            # SessionStateChanged → resolver returns None → fallback to
            # builder's channel_id ('ops-alerts' for paused).
            await bus.publish(
                SessionStateChanged(
                    session_id="s1", from_state="running", to_state="paused"
                )
            )
            await asyncio.sleep(0.05)
            assert backend.sent[1].channel_id == "ops-alerts"
        finally:
            await _stop(sub, task)


class TestErrorIsolation:
    async def test_builder_exception_logs_and_continues(self):
        """A builder crash on one event MUST NOT take down the
        subscriber — subsequent events still dispatch."""
        bus = EventBus()
        builder = _StubBuilder(raise_on_hitl=True)
        backend = _StubBackend()
        sub, task = await _start_subscriber(bus, builder, backend)
        try:
            await bus.publish(
                HITLRequested(
                    session_id="s1",
                    req_id="r1",
                    tool="bash",
                    args_redacted={},
                )
            )
            # Crash recorded, no card sent.
            await asyncio.sleep(0.05)
            assert backend.sent == []
            # Next event of a different type still flows.
            await bus.publish(
                SessionCompleted(session_id="s2", status="completed")
            )
            await asyncio.sleep(0.05)
            assert len(backend.sent) == 1
            assert backend.sent[0].payload["kind"] == "end"
        finally:
            await _stop(sub, task)

    async def test_backend_send_failure_swallowed(self):
        """IM delivery failures (network, 5xx) MUST NOT crash the
        subscriber — they're logged and processing continues."""
        bus = EventBus()
        builder = _StubBuilder()
        backend = _StubBackend(raise_on_send=RuntimeError("feishu 500"))
        sub, task = await _start_subscriber(bus, builder, backend)
        try:
            await bus.publish(
                SessionCompleted(session_id="s1", status="completed")
            )
            await asyncio.sleep(0.05)
            # Backend.sent stays empty because the stub raised, but the
            # subscriber didn't propagate the error.
            assert backend.sent == []
            # Repair the backend and publish again — second call succeeds.
            backend.raise_on_send = None
            await bus.publish(
                SessionCompleted(session_id="s2", status="completed")
            )
            await asyncio.sleep(0.05)
            assert len(backend.sent) == 1
        finally:
            await _stop(sub, task)


class TestProtocolGuard:
    async def test_backend_without_send_card_rejected(self):
        bus = EventBus()
        builder = _StubBuilder()

        class _Bad:
            name = "bad"

        with pytest.raises(TypeError, match="send_card"):
            IMSubscriber(bus=bus, builder=builder, backend=_Bad())  # type: ignore[arg-type]
