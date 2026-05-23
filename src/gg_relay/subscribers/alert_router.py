"""Alert routing with rule match + cooldown + mention resolve (Plan 8 D8.7).

The router is the *decision* layer between :class:`FailureSubscriber`
(which receives every terminal session transition) and the IM transport
(which physically sends the card). It implements three concerns that
otherwise would smear across the subscriber + the backend:

1. **Rule matching** — ``cfg.alert_rules`` ↦ :attr:`AlertRouter._rules`
   is a tiny DSL: ``{"fail":["always"], "cancel":["timeout"],
   "complete":["tag=notify"]}``. Each condition is one of:
     * ``"always"`` — match every event of this kind
     * ``"<end_reason>"`` — match when the terminal event's
       ``end_reason`` equals the literal (e.g. ``"timeout_recovered"``)
     * ``"tag=<name>"`` — match when ``<name>`` is in the session's
       ``tags`` array
2. **Cooldown** — per ``(event_type, owner, end_reason)`` tuple, a
   monotonic-clock TTL prevents repeat alerts from flooding the IM
   channel when a flaky session retries every 30 s. Default 5 min,
   capped to 1 000 entries via an LRU eviction so a bursty stream of
   distinct owners can't grow the dict unboundedly.
3. **Mention resolve** — ``cfg.feishu_user_mapping[owner] → open_id``
   is looked up at dispatch time and threaded into the card builder
   as ``mention_open_id``. The builder is responsible for the
   platform-specific ``<at id="…"></at>`` shape; an unknown owner
   (mapping miss) simply omits the mention, so the card still lands
   in the team channel without a @-ping.

**Multi-worker note (Plan 11+ TODO)**: cooldown state is in-process
memory. With ``N`` workers behind a load balancer, the same event
class can alert ``N`` times within ``cooldown_s`` if every worker sees
a distinct session that matches a rule. Documented in
``docs/team-deployment.md`` (#cooldown-multi-worker); the long-term
plan migrates state to Redis to share the LRU across workers.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any

from gg_relay.im.card import RenderedCard

logger = logging.getLogger("gg_relay.subscribers.alert_router")


class AlertRouter:
    """Match rules → cooldown → resolve mention → send card.

    The router is *transport-agnostic*: it accepts any object exposing
    ``async send_card(RenderedCard) -> None``. In production the
    lifespan wires :class:`gg_relay.im.backends.feishu.FeishuBackend`;
    tests pass a tiny stub backend that records every send.

    A missing :attr:`_backend` or :attr:`_card_builder` is *not* a
    hard error — a deployment with ``feishu_app_secret`` unset still
    boots, the router simply logs a warning and reports
    ``dispatched=False`` so the subscriber's caller can drop the event.
    """

    DEFAULT_RULES: dict[str, list[str]] = {
        "fail": ["always"],
        "cancel": ["timeout", "timeout_recovered", "paused_timeout"],
        "complete": ["tag=notify"],
    }
    """Plan 8 D8.7 defaults — fail always; cancel only on operational
    timeouts (never on operator-initiated ``user_request``); complete
    only when the session was explicitly tagged ``notify`` at submit
    time. Overridden by ``cfg.alert_rules`` per-key (merge, not
    replace — operators add ``complete: ["always"]`` to the JSON env
    without losing the fail/cancel coverage).
    """

    DEFAULT_COOLDOWN_S: int = 300
    LRU_CAP: int = 1000

    _EVENT_TYPE_TO_RULE_KEY: dict[str, str] = {
        "session_failed": "fail",
        "session_cancelled": "cancel",
        "session_completed": "complete",
    }

    def __init__(
        self,
        *,
        rules: dict[str, list[str]] | None = None,
        feishu_user_mapping: dict[str, str] | None = None,
        backend: Any = None,
        card_builder: Any = None,
        default_channel: str | None = None,
        cooldown_s: int = DEFAULT_COOLDOWN_S,
    ) -> None:
        merged: dict[str, list[str]] = {
            key: list(values) for key, values in self.DEFAULT_RULES.items()
        }
        if rules:
            for key, values in rules.items():
                merged[key] = list(values)
        self._rules = merged
        self._feishu_user_mapping = dict(feishu_user_mapping or {})
        self._backend = backend
        self._card_builder = card_builder
        self._default_channel = default_channel
        self._cooldown_s = cooldown_s
        self._last_alert: OrderedDict[tuple[str, str, str], float] = OrderedDict()
        self._lock = asyncio.Lock()

    @property
    def rules(self) -> dict[str, list[str]]:
        """Read-only view of the effective rule set (defaults + overrides)."""
        return {key: list(values) for key, values in self._rules.items()}

    def _matches(
        self, *, event_type: str, end_reason: str, tags: list[str]
    ) -> bool:
        """Apply the per-event-type rule list. First match wins.

        Returns ``False`` for unknown ``event_type`` (defensive — the
        subscriber should already have filtered, but a typo upstream
        shouldn't surface as a spurious alert).
        """
        rule_key = self._EVENT_TYPE_TO_RULE_KEY.get(event_type)
        if rule_key is None:
            return False
        conditions = self._rules.get(rule_key) or []
        tags_set = set(tags or [])
        for cond in conditions:
            if cond == "always":
                return True
            if cond.startswith("tag="):
                wanted = cond[4:].strip()
                if wanted and wanted in tags_set:
                    return True
            elif cond == end_reason:
                return True
        return False

    async def _cooldown_check(self, key: tuple[str, str, str]) -> bool:
        """Return ``True`` when the key may alert NOW, and record the
        send timestamp atomically. ``False`` means we're inside the
        per-key cooldown window — caller MUST skip dispatch.

        Cooldown granularity is the ``(event_type, owner, end_reason)``
        tuple so:
          * a flaky session run by ``alice`` that fails with
            ``http:502`` four times in five minutes alerts once;
          * the SAME session failing with a different ``end_reason``
            (e.g. ``timeout`` after the first retry's backoff) alerts
            again because the tuple differs — operators care about
            the change.

        LRU eviction caps memory at :attr:`LRU_CAP` entries so a
        bursty stream of distinct ``(owner, end_reason)`` pairs (a
        flapping CI matrix, say) can't grow the dict unboundedly.
        """
        async with self._lock:
            now = time.monotonic()
            last = self._last_alert.get(key)
            if last is not None and (now - last) < self._cooldown_s:
                return False
            if (
                key not in self._last_alert
                and len(self._last_alert) >= self.LRU_CAP
            ):
                self._last_alert.popitem(last=False)
            self._last_alert[key] = now
            self._last_alert.move_to_end(key)
            return True

    def resolve_mention(self, owner: str | None) -> str | None:
        """Map ``owner`` → Feishu ``open_id`` via :attr:`_feishu_user_mapping`.

        Returns ``None`` when the owner is unset or the mapping has
        no entry — the card builder must tolerate this and omit the
        ``<at id="…">`` element so the card still renders cleanly in
        the team channel.
        """
        if not owner:
            return None
        return self._feishu_user_mapping.get(owner)

    async def dispatch(
        self,
        *,
        event_type: str,
        session_id: str,
        owner: str | None,
        tags: list[str],
        end_reason: str,
        event: Any,
    ) -> bool:
        """Match → cooldown → resolve mention → send. Returns ``True``
        iff a card was actually handed to the backend.

        Failure modes (all return ``False`` and log at appropriate
        level; the EventBus consumer treats every return as success
        so a downed Feishu API NEVER stalls the bus):

          * No matching rule — debug log (expected for most events)
          * In cooldown — debug log
          * Missing backend / card_builder — warn log once per call
          * Backend ``send_card`` raised — warn log with traceback
        """
        if not self._matches(
            event_type=event_type, end_reason=end_reason, tags=tags
        ):
            return False
        key = (event_type, owner or "anon", end_reason)
        if not await self._cooldown_check(key):
            logger.debug(
                "alert in cooldown event_type=%s owner=%s end_reason=%s",
                event_type,
                owner,
                end_reason,
            )
            return False
        if self._backend is None or self._card_builder is None:
            logger.warning(
                "alert matched but no IM backend/card_builder wired "
                "(event_type=%s session_id=%s); skipping dispatch",
                event_type,
                session_id,
            )
            return False
        mention_open_id = self.resolve_mention(owner)
        try:
            card = self._card_builder.build_alert_card(
                event=event,
                event_type=event_type,
                session_id=session_id,
                owner=owner,
                end_reason=end_reason,
                mention_open_id=mention_open_id,
            )
        except Exception:
            logger.warning(
                "alert card_builder.build_alert_card raised "
                "event_type=%s session_id=%s",
                event_type,
                session_id,
                exc_info=True,
            )
            return False
        if (
            isinstance(card, RenderedCard)
            and card.channel_id is None
            and self._default_channel
        ):
            from dataclasses import replace

            card = replace(card, channel_id=self._default_channel)
        try:
            await self._backend.send_card(card)
        except Exception:
            logger.warning(
                "alert backend.send_card raised event_type=%s session_id=%s",
                event_type,
                session_id,
                exc_info=True,
            )
            return False
        return True
