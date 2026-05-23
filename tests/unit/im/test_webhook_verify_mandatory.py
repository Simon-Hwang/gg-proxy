"""Plan 7 Task 12 (D7.16) — verify_webhook is mandatory + async.

Three guards under test:

1. ``IMBackend`` is ``runtime_checkable`` and accepts the concrete
   :class:`FeishuBackend` as a member.
2. A backend that omits ``verify_webhook`` fails the isinstance check
   (Protocol structural typing catches the missing method).
3. :class:`IMSubscriber` refuses to construct when ``verify_webhook``
   exists but is synchronous — async is required so the route handler
   can ``await`` it consistently.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest
from pydantic import SecretStr

from gg_relay.config import Config
from gg_relay.core import EventBus
from gg_relay.im.backends.feishu import FeishuBackend
from gg_relay.im.card import RenderedCard
from gg_relay.im.protocol import IMBackend
from gg_relay.im.subscriber import IMSubscriber


def _cfg() -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.feishu_app_id = SecretStr("app")
    cfg.feishu_app_secret = SecretStr("sec")
    cfg.feishu_webhook_secret = SecretStr("whk")
    return cfg


@dataclass
class _GoodBuilder:
    def build_hitl_card(
        self, event: object, *, callback_base: str
    ) -> RenderedCard:
        del event, callback_base
        return RenderedCard(payload={})

    def build_session_end_card(self, event: object) -> RenderedCard:
        del event
        return RenderedCard(payload={})

    def build_session_state_card(self, event: object) -> RenderedCard:
        del event
        return RenderedCard(payload={})

    def build_other(self, event: object) -> RenderedCard | None:
        del event
        return None


@dataclass
class _AsyncOkBackend:
    """Smallest backend that satisfies the Plan 7 D7.16 contract."""

    name: str = "ok"
    sent: list[RenderedCard] = field(default_factory=list)

    async def send_card(self, card: RenderedCard) -> None:
        self.sent.append(card)

    async def verify_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> bool:
        del headers, body
        return True


@dataclass
class _SyncVerifyBackend:
    """verify_webhook defined as a plain ``def`` — must be rejected."""

    name: str = "sync"

    async def send_card(self, card: RenderedCard) -> None:
        del card

    def verify_webhook(  # NOTE: intentionally sync, not async
        self, headers: Mapping[str, str], body: bytes
    ) -> bool:
        del headers, body
        return True


class _NoVerifyBackend:
    """Bare-bones backend missing verify_webhook entirely."""

    name = "missing"

    async def send_card(self, card: RenderedCard) -> None:
        del card


@pytest.mark.asyncio
async def test_imbackend_protocol_runtime_checkable():
    """``IMBackend`` is ``@runtime_checkable``; FeishuBackend satisfies it."""
    backend = FeishuBackend(config=_cfg())
    try:
        assert isinstance(backend, IMBackend)
    finally:
        # FeishuBackend constructs an httpx client — close it so the
        # test doesn't leak the underlying connection pool.
        await backend.aclose()


def test_dummy_class_without_verify_webhook_isinstance_false():
    """Backends missing ``verify_webhook`` must fail the Protocol check."""
    bad = _NoVerifyBackend()
    assert not isinstance(bad, IMBackend), (
        "_NoVerifyBackend has no verify_webhook attribute and must NOT "
        "pass the runtime IMBackend isinstance check"
    )


def test_sync_verify_webhook_raises_typeerror():
    """Sync verify_webhook → TypeError at subscriber construction."""
    bus = EventBus()
    builder = _GoodBuilder()
    backend = _SyncVerifyBackend()
    with pytest.raises(TypeError, match="verify_webhook must be async"):
        IMSubscriber(
            bus=bus,
            builder=builder,  # type: ignore[arg-type]
            backend=backend,  # type: ignore[arg-type]
        )


def test_async_verify_webhook_accepted():
    """Async verify_webhook → subscriber constructs cleanly."""
    bus = EventBus()
    builder = _GoodBuilder()
    backend = _AsyncOkBackend()
    sub = IMSubscriber(
        bus=bus,
        builder=builder,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
    )
    assert sub.backend is backend


def test_backend_without_verify_webhook_raises_typeerror():
    """Missing verify_webhook entirely → TypeError at construction."""
    bus = EventBus()
    builder = _GoodBuilder()
    backend = _NoVerifyBackend()
    with pytest.raises(TypeError, match="verify_webhook"):
        IMSubscriber(
            bus=bus,
            builder=builder,  # type: ignore[arg-type]
            backend=backend,  # type: ignore[arg-type]
        )
