"""IMSubscriber — Plan 6 Task 6 / D6.8=A.

Glues the typed event bus to a :class:`CardBuilder` + :class:`IMBackend`
pair. Subscribes once for every event class the builder cares about
(HITL pending, session ended, state changed, …) and translates each
incoming :class:`RelayEvent` into a :class:`RenderedCard` that the
backend then dispatches.

D6.8=A introduces a ``channel_resolver`` hook — a pluggable callable
that maps an event to a destination channel id. In Plan 6 we ship
``channel_resolver=None`` (single default channel per backend); Plan
7+ can wire a multi-team router that dispatches "billing" tags to
``#billing-ops`` and HITL events to ``#approvers`` without changing
the subscriber.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from gg_relay.core import (
    EventBus,
    HITLRequested,
    RelayEvent,
    SessionCompleted,
    SessionStateChanged,
)
from gg_relay.im.card import CardBuilder, RenderedCard
from gg_relay.im.protocol import IMBackend

logger = logging.getLogger("gg_relay.im.subscriber")


ChannelResolver = Callable[[RelayEvent], str | None]
"""Returns the channel id this event should be routed to, or ``None``
to fall through to ``default_channel``. The resolver MUST be sync and
side-effect-free — IMSubscriber calls it on the bus consumer's hot
path."""


# Maps each typed event class to the CardBuilder method that renders it.
# Sync wrappers around the build_* methods so the subscriber's main
# loop can dispatch without isinstance ladders.
_BuilderFn = Callable[[CardBuilder, RelayEvent, str], RenderedCard | None]


def _build_hitl(
    builder: CardBuilder, event: RelayEvent, callback_base: str
) -> RenderedCard | None:
    if not isinstance(event, HITLRequested):
        return None
    return builder.build_hitl_card(event, callback_base=callback_base)


def _build_completed(
    builder: CardBuilder, event: RelayEvent, callback_base: str
) -> RenderedCard | None:
    del callback_base
    if not isinstance(event, SessionCompleted):
        return None
    return builder.build_session_end_card(event)


def _build_state(
    builder: CardBuilder, event: RelayEvent, callback_base: str
) -> RenderedCard | None:
    del callback_base
    if not isinstance(event, SessionStateChanged):
        return None
    return builder.build_session_state_card(event)


_DISPATCH: dict[type[RelayEvent], _BuilderFn] = {
    HITLRequested: _build_hitl,
    SessionCompleted: _build_completed,
    SessionStateChanged: _build_state,
}


@dataclass
class IMSubscriber:
    """Bus → CardBuilder → IMBackend pipeline.

    Construction is cheap; call :meth:`run` (typically wrapped in
    ``asyncio.create_task`` from the API lifespan) to start draining.
    :meth:`stop` is idempotent and waits for every dispatcher subtask
    to complete so the lifespan teardown is deterministic.
    """

    bus: EventBus
    builder: CardBuilder
    backend: IMBackend
    default_channel: str | None = None
    public_callback_base: str = ""
    channel_resolver: ChannelResolver | None = None
    """Optional per-event router. ``None`` (Plan 6 default) sends
    everything to ``default_channel``; supply a callable for Plan 7+
    multi-team routing. The resolver's return value takes precedence
    over the builder's ``RenderedCard.channel_id`` — explicit > implicit.
    """

    _tasks: list[asyncio.Task[None]] = field(default_factory=list, init=False)
    _stopped: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        # Module-level sanity assert so a missing IMBackend.send_card is
        # caught at wiring time, not at the first event.
        if not hasattr(self.backend, "send_card"):
            raise TypeError(
                "IMSubscriber requires backend.send_card; "
                f"got {type(self.backend).__name__}"
            )
        # Plan 7 D7.16: verify_webhook is mandatory AND must be async.
        # A sync `def verify_webhook(...)` would be a silent footgun —
        # FastAPI would still await its return value (a coroutine? no,
        # a bare bool) by accident, but the bigger risk is contracts
        # that lie. We reject the backend at construction time so a
        # misconfigured deployment never reaches a live request path.
        verify = getattr(self.backend, "verify_webhook", None)
        if verify is None:
            raise TypeError(
                "IMSubscriber requires backend.verify_webhook; "
                f"got {type(self.backend).__name__}"
            )
        if not inspect.iscoroutinefunction(verify):
            raise TypeError(
                f"{type(self.backend).__name__}.verify_webhook must be "
                f"async (got {type(verify).__name__})"
            )

    async def run(self) -> None:
        """Spawn one consumer per registered event class and block
        until cancelled or :meth:`stop` is called.

        Iterators are created up-front (synchronous registration via
        :meth:`EventBus.subscribe`) so the subscriptions are live by
        the time we return from this method's prelude — callers can
        publish immediately without races against ``asyncio.sleep(0)``.
        """
        iterators = [
            (self.bus.subscribe(ev_cls), ev_cls, builder_fn)
            for ev_cls, builder_fn in _DISPATCH.items()
        ]
        for iterator, ev_cls, builder_fn in iterators:
            task = asyncio.create_task(
                self._consume_loop(iterator, builder_fn),
                name=f"im-subscriber-{ev_cls.__name__}",
            )
            self._tasks.append(task)
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            await self._cancel_all()
            raise

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        await self._cancel_all()

    async def _cancel_all(self) -> None:
        for task in self._tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException) and not isinstance(
                r, asyncio.CancelledError
            ):
                logger.warning("im subscriber subtask error: %s", r)

    async def _consume_loop(
        self,
        iterator: Any,
        builder_fn: _BuilderFn,
    ) -> None:
        """Per-event-class consumer. The iterator is pre-supplied by
        :meth:`run` so subscription registration happens synchronously
        before the consumer task starts — avoids "publish before
        subscribe" races in tests/lifespan startup.

        The Plan 5 typed bus delivers a single class's events in order;
        we render + dispatch each one in turn so within a class the
        ordering matches publish order. Different classes get parallel
        pipelines (separate tasks) so a slow Feishu HTTP call never
        blocks a HITL card.
        """
        async for event in iterator:
            try:
                card = builder_fn(
                    self.builder, event, self.public_callback_base
                )
            except Exception:
                logger.exception(
                    "card builder raised for %s", type(event).__name__
                )
                continue
            if card is None:
                continue
            await self._dispatch(event, card)

    async def _dispatch(self, event: RelayEvent, card: RenderedCard) -> None:
        """Decide the destination channel, then call backend.send_card.

        Channel resolution (highest priority first):
          1. ``channel_resolver(event)`` if supplied and non-None
          2. ``card.channel_id`` if the builder picked one explicitly
          3. ``default_channel`` (lifespan-supplied)
          4. ``None`` — let the backend decide / drop with a warning
        """
        resolved: str | None = None
        if self.channel_resolver is not None:
            try:
                resolved = self.channel_resolver(event)
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "channel_resolver raised for %s; falling back",
                    type(event).__name__,
                )
        if resolved is None:
            resolved = card.channel_id
        if resolved is None:
            resolved = self.default_channel
        # Replace channel_id on the card so the backend always sees the
        # final routing decision (avoids backends having to consult
        # configuration themselves).
        final_card = (
            card if card.channel_id == resolved else replace(card, channel_id=resolved)
        )
        try:
            await self.backend.send_card(final_card)
        except Exception as exc:
            # IM delivery failures are NEVER fatal — we don't want a
            # downed Feishu API to crash gg-relay. Log and move on.
            logger.warning(
                "IM backend %s failed to send card for %s: %s",
                getattr(self.backend, "name", type(self.backend).__name__),
                type(event).__name__,
                exc,
            )


