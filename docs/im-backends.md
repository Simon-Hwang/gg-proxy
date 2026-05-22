# IM Backends

> Plan 6 D6.7 + D6.8 — gg-relay's IM (instant-messaging) integration is
> decoupled into a `CardBuilder` Protocol that *renders* events into
> platform-agnostic cards and an `IMBackend` that *transports* them.
> This guide explains the abstraction, the Plan-6-shipped Feishu
> implementation, and how to add a new backend (DingTalk, Slack,
> 企微, Teams, …) in Plan 7+.

## Architecture

```
EventBus (typed RelayEvent fan-out)
    │
    ▼
IMSubscriber.run()
    │  ─ subscribes to "*" (wildcard)
    │  ─ for each event:
    │       card = builder.build_<event-shape>(event)
    │       channel = resolver(event) or default_channel
    │       backend.send_card(channel, card)
    ▼
CardBuilder  ──renders──▶  RenderedCard  ──transports──▶  IMBackend
(Feishu / DingTalk / Slack / …)                          (HTTP API)
```

* **`CardBuilder`** (`gg_relay.im.card.CardBuilder`) — pure renderer.
  Knows nothing about HTTP, secrets, or send-card semantics. Three
  required builders (`build_hitl_card`, `build_session_end_card`,
  `build_session_state_card`) plus a default-`None` `build_other`
  fallback for forward-compat with future event types.
* **`IMBackend`** (`gg_relay.im.protocol.IMBackend`) — transport layer.
  Knows the platform's auth, signing, retry semantics, and the
  webhook-verification path. The card content is opaque.
* **`IMSubscriber`** (`gg_relay.im.subscriber.IMSubscriber`) — glue.
  Binds the EventBus to a `(CardBuilder, IMBackend)` pair, handles
  per-event routing through an optional `channel_resolver` closure,
  and isolates send-card failures so one slow consumer can't kill
  the subscriber loop.

## Adding a new backend

Below is a minimal recipe for hooking up a hypothetical Slack backend.
The key insight is that the renderer and the transport are independently
testable — you can ship the `CardBuilder` first with unit tests and
swap in the real HTTP backend later.

### 1. Implement `CardBuilder`

```python
# src/gg_relay/im/backends/slack.py
from gg_relay.core import (
    HITLRequested, SessionCompleted, SessionStateChanged, RelayEvent,
)
from gg_relay.im.card import CardBuilder, CardAction, RenderedCard


class SlackCardBuilder:
    name = "slack"

    def build_hitl_card(
        self, event: HITLRequested, *, callback_base: str
    ) -> RenderedCard | None:
        return RenderedCard(
            title=f"HITL approval required — {event.tool}",
            body_markdown=f"Session `{event.session_id}` waiting on `{event.tool}`",
            actions=(
                CardAction(label="Approve", decision="accept", payload={
                    "session_id": event.session_id,
                    "req_id": event.req_id,
                    "callback": f"{callback_base}/im/slack/callback",
                }, style="primary"),
                CardAction(label="Deny", decision="deny", payload={
                    "session_id": event.session_id,
                    "req_id": event.req_id,
                    "callback": f"{callback_base}/im/slack/callback",
                }, style="danger"),
            ),
            color="yellow",
        )

    def build_session_end_card(
        self, event: SessionCompleted
    ) -> RenderedCard | None:
        return RenderedCard(
            title=f"Session {event.session_id} {event.status}",
            body_markdown=(
                f"Tokens: in={event.tokens.get('in', 0)} "
                f"out={event.tokens.get('out', 0)} "
                f"cost=${event.cost_usd:.4f}"
            ),
            color="green" if event.status == "completed" else "red",
        )

    def build_session_state_card(
        self, event: SessionStateChanged
    ) -> RenderedCard | None:
        # Only notify on user-relevant transitions; silence the rest.
        if event.to_state in {"paused", "cancelled"}:
            return RenderedCard(
                title=f"Session {event.session_id} → {event.to_state}",
                body_markdown=event.reason or "(no reason)",
                color="yellow" if event.to_state == "paused" else "red",
            )
        return None

    def build_other(self, event: RelayEvent) -> RenderedCard | None:
        return None
```

### 2. Implement `IMBackend`

```python
class SlackBackend:
    def __init__(self, *, bot_token: str, http: httpx.AsyncClient) -> None:
        self._bot = bot_token
        self._http = http
        self.builder = SlackCardBuilder()  # convenience attr

    async def send_card(
        self, *, channel: str, card: RenderedCard
    ) -> None:
        # Translate RenderedCard → Slack blocks API + post.
        ...

    def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> bool:
        # Slack signed-secret verification.
        ...
```

### 3. Wire into the lifespan

In `src/gg_relay/api/main.py` `lifespan()`:

```python
if cfg.slack_bot_token:
    slack_backend = SlackBackend(
        bot_token=cfg.slack_bot_token.get_secret_value(),
        http=httpx.AsyncClient(),
    )
    slack_subscriber = IMSubscriber(
        bus=bus,
        builder=slack_backend.builder,
        backend=slack_backend,
        default_channel=cfg.slack_default_channel,
        public_callback_base=cfg.public_base_url,
        channel_resolver=None,  # Plan 7+ multi-team router
    )
    bg_tasks.append(asyncio.create_task(slack_subscriber.run(), name="im-slack"))
```

### 4. Tests

* Unit: `tests/unit/im/test_slack_card_builder.py` — verify each
  builder emits the right `RenderedCard` shape (titles, actions,
  colors).
* Unit: `tests/unit/im/test_slack_backend.py` — mock `httpx` (via
  `respx`) and verify `send_card` translates correctly and
  `verify_webhook` rejects bad signatures.
* Integration: drop `IMSubscriber` into a live `EventBus` and assert
  the right backend method is called for each published event.

## Channel routing (Plan 7+)

The `IMSubscriber` constructor takes an optional `channel_resolver:
Callable[[RelayEvent], str | None]`. In Plan 6 the closure is always
`None` and every event goes to `default_channel`. Plan 7's
multi-team support fills it in:

```python
# Resolve session.tags → channel id via a config map.
def _resolver(event: RelayEvent) -> str | None:
    if not isinstance(event, SessionCreated):
        return None  # other events fall back to default
    for tag in event.tags:
        if (channel := cfg.tag_to_channel.get(tag)) is not None:
            return channel
    return None
```

The resolver MUST be side-effect-free; if it raises, the subscriber
logs and falls back to the default channel so a misconfigured map
doesn't black-hole notifications.

## Webhook verification

Inbound webhook handlers (HITL decisions clicked in the IM client)
remain on the existing routing path:

```
client click → IM platform webhook → /im/<platform>/callback →
    backend.verify_webhook(payload, headers) → on ok: HITLCoordinator.resolve()
```

The `IMBackend` subset relevant here (`verify_webhook`) is
intentionally retained on the protocol so each backend owns its
signing scheme. Adding a new backend means adding both the outbound
`send_card` translation AND the inbound webhook verification +
router entry under `src/gg_relay/im/backends/<name>.py`.
