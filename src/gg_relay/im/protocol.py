"""Plugin-style protocol for IM backends.

Plan 6 D6.7=C narrowed the contract: backends are now responsible *only*
for transport (auth, token caching, retry, rate-limit). Rendering is
delegated to a separate :class:`~gg_relay.im.card.CardBuilder`. The
:class:`~gg_relay.im.subscriber.IMSubscriber` wires the two together by
subscribing to the typed event bus, calling the builder, and handing
the resulting :class:`~gg_relay.im.card.RenderedCard` to the backend's
``send_card`` method.

Two legacy convenience methods (``notify_hitl_pending`` /
``notify_session_end``) remain on the Protocol with default async stubs
so the migration from Plan 4's IMBackend can land in one commit per
backend without breaking type-checking against the old call sites.
The default stubs delegate to ``send_card`` when callers wire a
builder, and raise NotImplementedError otherwise so silent regressions
are loud.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from gg_relay.im.card import RenderedCard


@runtime_checkable
class IMBackend(Protocol):
    """Outbound messenger surface (Feishu, future Slack, etc.).

    Plan 6 narrowed this to a single primary method ``send_card``;
    legacy ``notify_*`` methods are kept as Protocol-default stubs so
    SessionManager call sites still type-check during the Task 7
    migration. New backends only need to implement ``send_card``.
    """

    name: str

    async def send_card(self, card: RenderedCard) -> None:
        """Dispatch a pre-rendered card to the target IM platform.

        The default channel comes from the lifespan-supplied config;
        per-card overrides land in :attr:`RenderedCard.channel_id`.
        Backends MUST treat ``card.payload`` as already-validated and
        send it verbatim — gg-relay's redactor has already scrubbed
        sensitive values before the bus fan-out.
        """
        ...

    # Legacy compat — see module docstring. Concrete backends MAY
    # override either or both; the IMSubscriber never calls them.
    async def notify_hitl_pending(
        self,
        *,
        session_id: str,
        req_id: str,
        tool: str,
        args_summary: str,
        callback_base: str,
    ) -> None: ...

    async def notify_session_end(
        self,
        *,
        session_id: str,
        status: str,
        summary: str,
    ) -> None: ...
