"""Feishu (Lark) IM backend — split into :class:`FeishuCardBuilder`
(pure rendering) and :class:`FeishuBackend` (transport) per Plan 6
D6.7=C / Task 7.

Builder is a :class:`gg_relay.im.card.CardBuilder` implementation
producing Feishu interactive-card JSON. Backend implements
:class:`gg_relay.im.protocol.IMBackend`'s ``send_card`` plus the
Plan 7 D7.16 mandatory ``verify_webhook`` — the legacy
``notify_hitl_pending`` / ``notify_session_end`` shims are kept as
thin wrappers around the builder + send_card for any callers that
haven't migrated yet (production calls now go through
:class:`gg_relay.im.subscriber.IMSubscriber`).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from gg_relay.config import Config
from gg_relay.core import (
    HITLRequested,
    RelayEvent,
    SessionCompleted,
    SessionStateChanged,
)
from gg_relay.im.card import CardAction, RenderedCard

_FEISHU_BASE_URL = "https://open.feishu.cn"


def verify_feishu_signature(
    *,
    timestamp: str,
    secret: str,
    received: str | None,
) -> bool:
    """Verify a Feishu webhook signature.

    Algorithm matches the Feishu custom-bot interactive-callback spec:
    HMAC-SHA256 with signing key ``timestamp + "\\n" + secret`` over an
    EMPTY message, base64-encoded. Exposed as a pure function so unit
    tests can construct expected vectors with hand-computed values.
    """
    if not received:
        return False
    key = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(key, b"", hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, received)


class FeishuCardBuilder:
    """Pure synchronous renderer for Feishu interactive cards.

    All four ``build_*`` methods produce dict payloads matching the
    Feishu *open.message.interactive* schema. Tests snapshot the output
    directly so any schema drift surfaces immediately.
    """

    def build_hitl_card(
        self, event: HITLRequested, *, callback_base: str
    ) -> RenderedCard:
        """Render an actionable HITL card with Approve / Deny buttons.

        ``callback_base`` is included for forward-compat with callback
        modes where the button value would carry a one-shot URL; the
        current Feishu impl encodes session_id+req_id+decision inline,
        which the existing webhook router already understands.
        """
        del callback_base
        args_summary = _summarise_args(event.args_redacted)
        body = args_summary[:512]
        payload: dict[str, Any] = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"HITL: {event.tool}"},
                "template": "yellow",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**Session**: `{event.session_id}`\n"
                        f"**req_id**: `{event.req_id}`\n"
                        f"**Args**:\n```\n{body}\n```"
                    ),
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "Approve",
                            },
                            "type": "primary",
                            "value": {
                                "session_id": event.session_id,
                                "req_id": event.req_id,
                                "decision": "accept",
                            },
                        },
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "Deny",
                            },
                            "type": "danger",
                            "value": {
                                "session_id": event.session_id,
                                "req_id": event.req_id,
                                "decision": "deny",
                            },
                        },
                    ],
                },
            ],
        }
        return RenderedCard(
            payload=payload,
            actions=(
                CardAction(
                    label="Approve",
                    payload={
                        "session_id": event.session_id,
                        "req_id": event.req_id,
                        "decision": "accept",
                    },
                    style="primary",
                ),
                CardAction(
                    label="Deny",
                    payload={
                        "session_id": event.session_id,
                        "req_id": event.req_id,
                        "decision": "deny",
                    },
                    style="danger",
                ),
            ),
            metadata={"msg_type": "interactive"},
        )

    def build_session_end_card(self, event: SessionCompleted) -> RenderedCard:
        """Render the informational text message for a session terminal
        transition. Uses ``msg_type=text`` rather than an interactive
        card — there's nothing actionable to do at this point.
        """
        cost_part = (
            f" (${event.cost_usd:.4f})" if event.cost_usd else ""
        )
        text = f"[{event.status}] {event.session_id}{cost_part}"
        return RenderedCard(
            payload={"text": text},
            metadata={"msg_type": "text"},
        )

    def build_session_state_card(
        self, event: SessionStateChanged
    ) -> RenderedCard:
        """Render a state-transition notification — Plan 6 surfaces
        RUNNING ↔ PAUSED moves so operators see pause/resume in chat
        without polling the dashboard. We deliberately keep it lean
        (plain text) so the channel doesn't get spammed with rich cards
        on every transition.
        """
        reason_part = f" ({event.reason})" if event.reason else ""
        text = (
            f"`{event.session_id}` → {event.to_state} "
            f"(from {event.from_state}){reason_part}"
        )
        return RenderedCard(
            payload={"text": text},
            metadata={"msg_type": "text"},
        )

    def build_other(self, event: RelayEvent) -> RenderedCard | None:
        del event
        return None

    # ── Plan 8 D8.7 — AlertRouter card ───────────────────────────────

    _ALERT_TITLES: dict[str, str] = {
        "session_failed": "[ALERT] Session failed",
        "session_cancelled": "[WARN] Session cancelled",
        "session_completed": "[OK] Session completed",
    }
    _ALERT_TEMPLATES: dict[str, str] = {
        "session_failed": "red",
        "session_cancelled": "orange",
        "session_completed": "green",
    }

    def build_alert_card(
        self,
        *,
        event: Any,
        event_type: str,
        session_id: str,
        owner: str | None,
        end_reason: str,
        mention_open_id: str | None = None,
    ) -> RenderedCard:
        """Render the AlertRouter card (Plan 8 D8.7).

        Distinct from :meth:`build_session_end_card` (the noisy
        every-terminal-event surface used by :class:`IMSubscriber`)
        because this card is **actionable** for the on-call: it
        carries an explicit alert header colour, the resolved
        ``@mention`` of the session owner (when available via
        ``cfg.feishu_user_mapping``), and a "View" button linking to
        the dashboard so the operator can pivot to the full session
        detail with one tap.

        ``mention_open_id`` is the pre-resolved Feishu ``open_id``
        from :meth:`AlertRouter.resolve_mention`; when ``None`` the
        ``<at>`` element is omitted so the card still renders cleanly
        in the team channel (just without a notification ping).
        """
        del event  # signature compat; values come from explicit kwargs
        title = self._ALERT_TITLES.get(event_type, "[NOTE] Session event")
        template = self._ALERT_TEMPLATES.get(event_type, "blue")
        owner_display = owner or "anon"
        body_lines = [
            f"**Session:** `{session_id}`",
            f"**Owner:** {owner_display}",
            f"**Reason:** {end_reason}",
        ]
        if mention_open_id:
            body_lines.insert(
                0, f'<at id="{mention_open_id}"></at>'
            )
        payload: dict[str, Any] = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": "\n".join(body_lines),
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "View",
                            },
                            "type": "primary",
                            "url": f"/dashboard/sessions/{session_id}",
                        }
                    ],
                },
            ],
        }
        return RenderedCard(
            payload=payload,
            metadata={
                "msg_type": "interactive",
                "alert_event_type": event_type,
                "mention_open_id": mention_open_id or "",
            },
        )


def _summarise_args(args: dict[str, Any]) -> str:
    """Compact one-line representation of redacted args. JSON is the
    canonical form so the operator can copy-paste it into a debugger.
    Truncation happens in the caller."""
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return repr(args)


@dataclass
class _TokenCache:
    token: str = ""
    expires_at: float = 0.0


@dataclass
class FeishuBackend:
    """Transport-only Feishu backend (Plan 6 Task 7).

    Provides ``send_card`` for the new :class:`IMSubscriber` path plus
    legacy ``notify_*`` wrappers that build a card on the fly for any
    direct callers (currently none in-tree). Token caching matches the
    Plan-4 behaviour: a single tenant_access_token is reused until 60s
    before its expiry, then refreshed.
    """

    config: Config
    base_url: str = _FEISHU_BASE_URL
    http: httpx.AsyncClient = field(init=False)
    name: str = "feishu"
    _builder: FeishuCardBuilder = field(
        default_factory=FeishuCardBuilder, repr=False
    )
    _token: _TokenCache = field(default_factory=_TokenCache, repr=False)

    def __post_init__(self) -> None:
        self.http = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)

    async def aclose(self) -> None:
        await self.http.aclose()

    # ── new (Plan 6) primary surface ─────────────────────────────────

    async def verify_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> bool:
        """Verify an inbound Feishu interactive-callback signature.

        Plan 7 D7.16: returns ``False`` whenever ``feishu_webhook_secret``
        is unset/empty — no silent pass-through. The body is intentionally
        unused for the timestamp-keyed variant Feishu mandates for custom
        bots; we accept it on the signature so the Protocol stays stable
        across backends that DO sign over the body.
        """
        del body
        secret = (
            self.config.feishu_webhook_secret.get_secret_value()
            if self.config.feishu_webhook_secret
            else ""
        )
        if not secret:
            return False
        timestamp = headers.get("X-Lark-Request-Timestamp", "")
        received = headers.get("X-Lark-Signature")
        return verify_feishu_signature(
            timestamp=timestamp, secret=secret, received=received
        )

    async def send_card(self, card: RenderedCard) -> None:
        """POST a :class:`RenderedCard` to Feishu.

        Looks up the receive_id from ``card.channel_id`` (falling back
        to ``config.feishu_target_chat_id`` when unset) and picks
        ``msg_type`` from the card metadata (``"interactive"`` for
        actionable cards, ``"text"`` for plain messages).
        """
        token = await self._tenant_token()
        receive_id = card.channel_id or self.config.feishu_target_chat_id or ""
        msg_type = str(card.metadata.get("msg_type") or "interactive")
        content_payload: Any = card.payload
        await self.http.post(
            "/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": receive_id,
                "msg_type": msg_type,
                "content": json.dumps(content_payload),
            },
        )

    # ── builder accessor (used by tests + the lifespan wiring) ───────

    @property
    def builder(self) -> FeishuCardBuilder:
        return self._builder

    # ── legacy compatibility ─────────────────────────────────────────
    # Kept so any out-of-tree caller still calling notify_hitl_pending
    # / notify_session_end keeps working through the v0.6 migration.
    # IMSubscriber NEVER calls these.

    def build_hitl_card(
        self,
        *,
        session_id: str,
        req_id: str,
        tool: str,
        args_summary: str,
    ) -> dict[str, Any]:
        """LEGACY: callers that built cards via the backend directly
        (Plan 4-era tests) should migrate to
        :class:`FeishuCardBuilder.build_hitl_card`. Returned shape is
        identical."""
        event = HITLRequested(
            session_id=session_id,
            req_id=req_id,
            tool=tool,
            args_redacted={"_summary": args_summary},
        )
        # Build using the canonical builder but unwrap to dict so the
        # legacy assertion-style ("card['elements']") keeps working.
        # The args_redacted={"_summary": args_summary} round-trips into
        # the markdown body; we override that body so the legacy
        # snapshot test sees the original raw string.
        rendered = self._builder.build_hitl_card(event, callback_base="")
        payload = dict(rendered.payload)
        for el in payload.get("elements", []):
            if el.get("tag") == "markdown":
                el["content"] = (
                    f"**Session**: `{session_id}`\n"
                    f"**req_id**: `{req_id}`\n"
                    f"**Args**:\n```\n{args_summary[:512]}\n```"
                )
        return payload

    async def notify_hitl_pending(
        self,
        *,
        session_id: str,
        req_id: str,
        tool: str,
        args_summary: str,
        callback_base: str,
    ) -> None:
        del callback_base
        token = await self._tenant_token()
        card_payload = self.build_hitl_card(
            session_id=session_id,
            req_id=req_id,
            tool=tool,
            args_summary=args_summary,
        )
        target = self.config.feishu_target_chat_id or ""
        await self.http.post(
            "/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": target,
                "msg_type": "interactive",
                "content": json.dumps(card_payload),
            },
        )

    async def notify_session_end(
        self,
        *,
        session_id: str,
        status: str,
        summary: str,
    ) -> None:
        token = await self._tenant_token()
        target = self.config.feishu_target_chat_id or ""
        await self.http.post(
            "/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": target,
                "msg_type": "text",
                "content": json.dumps(
                    {"text": f"[{status}] {session_id}\n{summary[:512]}"}
                ),
            },
        )

    # ── auth (unchanged from Plan 4) ─────────────────────────────────

    async def _tenant_token(self) -> str:
        now = time.time()
        if self._token.token and self._token.expires_at > now + 60:
            return self._token.token
        app_id = (
            self.config.feishu_app_id.get_secret_value()
            if self.config.feishu_app_id
            else ""
        )
        secret = (
            self.config.feishu_app_secret.get_secret_value()
            if self.config.feishu_app_secret
            else ""
        )
        if not app_id or not secret:
            raise RuntimeError("feishu_app_id / feishu_app_secret unset")
        r = await self.http.post(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": secret},
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("code") != 0:
            raise RuntimeError(
                f"feishu token error: {payload.get('msg')!r}"
            )
        self._token = _TokenCache(
            token=payload["tenant_access_token"],
            expires_at=now + payload.get("expire", 7200),
        )
        return self._token.token
