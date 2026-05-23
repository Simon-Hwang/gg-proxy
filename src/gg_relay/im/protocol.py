"""Plugin-style protocol for IM backends.

Plan 6 D6.7=C narrowed the contract: backends are now responsible *only*
for transport (auth, token caching, retry, rate-limit). Rendering is
delegated to a separate :class:`~gg_relay.im.card.CardBuilder`. The
:class:`~gg_relay.im.subscriber.IMSubscriber` wires the two together by
subscribing to the typed event bus, calling the builder, and handing
the resulting :class:`~gg_relay.im.card.RenderedCard` to the backend's
``send_card`` method.

Plan 7 D7.16 promotes ``verify_webhook`` to a mandatory async method:
the previous "no-op signature verifier" pattern silently let
unauthenticated callbacks through whenever an operator forgot to set
``feishu_webhook_secret``. The new contract is:

* every backend MUST implement ``verify_webhook(headers, body) -> bool``
* the method MUST be ``async`` — :class:`IMSubscriber` fails fast at
  construction time when it is not, so a misconfigured backend never
  reaches a live request path
* an unset/empty secret MUST return ``False`` (no silent pass-through)

Two legacy convenience methods (``notify_hitl_pending`` /
``notify_session_end``) remain on the Protocol with default async stubs
so the migration from Plan 4's IMBackend can land in one commit per
backend without breaking type-checking against the old call sites.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from gg_relay.im.card import RenderedCard


@runtime_checkable
class IMBackend(Protocol):
    """Outbound messenger surface (Feishu, future Slack, etc.).

    Plan 6 narrowed this to ``send_card`` as the primary outbound
    method. Plan 7 D7.16 makes ``verify_webhook`` mandatory so the
    inbound side also has a single, auditable surface — no more
    silent pass-through when an operator forgets the webhook secret.
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

    async def verify_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> bool:
        """Verify an inbound webhook signature (Plan 7 D7.16).

        MUST be ``async`` — :class:`IMSubscriber` checks this with
        :func:`inspect.iscoroutinefunction` at construction time so a
        synchronous stub never reaches a live request path.

        MUST return ``False`` whenever the signing secret is missing
        or empty — silent pass-through is the original D7.16 footgun
        and is rejected at the contract level, not just by individual
        callers.
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
