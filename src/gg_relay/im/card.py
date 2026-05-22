"""IM card builder Protocol — Plan 6 Task 5 / D6.7=(C).

Decouples the *shape* of the rendered message from the *transport* that
sends it. The old contract (``IMBackend.notify_hitl_pending`` /
``notify_session_end``) collapsed both responsibilities into one class,
making it impossible to swap rendering (Feishu-card-v2, Slack
block-kit, Discord embed, …) without writing a whole new backend.

The new contract splits them in three:

1. :class:`CardBuilder` (this module) — pure, sync, tested by snapshot
   assertions. Given a typed :class:`RelayEvent`, returns a
   :class:`RenderedCard` carrying the platform-specific payload plus
   the channel hint and any clickable :class:`CardAction` buttons.
2. :class:`~gg_relay.im.protocol.IMBackend` — narrowed to a single
   ``send_card`` method (Plan 6 Task 7). Backend-specific concerns
   (auth, token caching, retry) live here.
3. :class:`~gg_relay.im.subscriber.IMSubscriber` (Plan 6 Task 6) —
   glues the two together by subscribing to the typed event bus and
   dispatching ``builder.build_X(event) → backend.send_card(...)``.

The Protocol provides four ``build_*`` methods. Three are MANDATORY
(``build_hitl_card`` / ``build_session_end_card`` /
``build_session_state_card``); the fourth (``build_other``) is
OPTIONAL — it returns ``None`` from the default implementation so a
builder can opt-out of rendering unknown event types without raising.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from gg_relay.core import (
    HITLRequested,
    RelayEvent,
    SessionCompleted,
    SessionStateChanged,
)


@dataclass(frozen=True, slots=True)
class CardAction:
    """A clickable button / link element on a rendered card.

    The ``payload`` is round-tripped through the IM platform's
    interactive-message envelope (Feishu ``value``, Slack ``value``,
    Discord ``custom_id`` etc.) back to gg-relay's webhook router so the
    subsequent HITL decision is signed and idempotent. ``style`` is a
    free-form hint (``"primary"`` / ``"danger"`` / ``"link"``) that
    the builder MAY use to drive platform-specific button colours.
    """

    label: str
    payload: dict[str, Any]
    style: str = "default"


@dataclass(frozen=True, slots=True)
class RenderedCard:
    """A platform-specific message ready for an IMBackend to dispatch.

    The ``payload`` is the raw body the backend will POST (Feishu
    interactive-card JSON, Slack blocks dict, etc.) — the subscriber
    treats it as opaque. ``channel_id`` is the destination identifier
    in the target platform's vocabulary (Feishu chat_id, Slack channel
    id, …); a ``None`` channel means "use the backend's default
    channel" (the lifespan-supplied default). ``actions`` is a separate
    list for backends that prefer to attach action-button metadata
    out-of-band from the payload, e.g. for audit logging.
    """

    payload: dict[str, Any]
    channel_id: str | None = None
    actions: tuple[CardAction, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class CardBuilder(Protocol):
    """Pure, sync card-payload renderer (Plan 6 D6.7=C).

    Implementations MUST be deterministic — given the same event they
    MUST produce equal :class:`RenderedCard` instances. The Protocol is
    intentionally narrow: each method handles ONE event type so
    extending the bus with a new typed event triggers a mypy break in
    every concrete builder instead of silently falling through to a
    catch-all.

    ``build_other`` is the documented escape hatch for events the
    builder doesn't care about — return ``None`` and the subscriber
    will skip the dispatch. The default implementation already does
    this, so most builders only need to implement the three required
    methods.
    """

    def build_hitl_card(
        self,
        event: HITLRequested,
        *,
        callback_base: str,
    ) -> RenderedCard:
        """Render the actionable card for a HITL pending event.

        Plan 5 named the typed event :class:`HITLRequested` (was
        ``HITLPending`` in early Plan-4 drafts); the spec for D6.7
        still uses the legacy name in prose. We standardise on the
        Plan-5 class name everywhere in the codebase."""
        ...

    def build_session_end_card(
        self,
        event: SessionCompleted,
    ) -> RenderedCard:
        """Render the informational card sent when a session terminates.

        Bound to :class:`SessionCompleted` (Plan 5 D5.11) which fires
        on every terminal transition regardless of status — the builder
        inspects ``event.status`` to decide tone (green for completed,
        red for failed, etc.)."""
        ...

    def build_session_state_card(
        self,
        event: SessionStateChanged,
    ) -> RenderedCard:
        """Render the state-change notification (Plan 6 uses this for
        RUNNING ↔ PAUSED transitions so operators can see pause/resume
        in their IM channel without polling the dashboard)."""
        ...

    def build_other(
        self,
        event: RelayEvent,
    ) -> RenderedCard | None:
        """Default no-op for unknown event types. Override if you want
        to surface tool decisions / progress chunks / etc."""
        return None
