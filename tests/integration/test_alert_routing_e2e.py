"""End-to-end alert routing — Plan 8 Task 11 (D8.7).

Spins up a real FastAPI app (so the lifespan wires AlertRouter +
FailureSubscriber), injects a spy backend onto ``app.state`` so the
typed event bus → router → backend path can be observed without a
live Feishu HTTP call, then publishes terminal
:class:`SessionStateChanged` events directly on the bus.

The store is seeded with sessions matching the cfg-driven alert
rules so each scenario exercises a different code path:

* ``test_failed_session_triggers_alert_card`` — default rule
  ``fail: always`` → any failed terminal fires
* ``test_user_cancel_no_alert`` — user-initiated cancel filter
  (``reason="user_request"``) → router never invoked
* ``test_complete_tag_notify_alert`` — D8.23 ``complete: ["tag=notify"]``
  → completed session tagged ``notify`` fires a green card
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.core import SessionStateChanged
from gg_relay.im.card import RenderedCard
from gg_relay.store import SessionRepository, create_all_tables, make_async_engine

pytestmark = pytest.mark.asyncio


@dataclass
class _SpyBackend:
    """Records every send_card call. Implements the IMBackend duck
    so :class:`AlertRouter` accepts it transparently."""

    name: str = "spy"
    sent: list[RenderedCard] = field(default_factory=list)

    async def send_card(self, card: RenderedCard) -> None:
        self.sent.append(card)


def _cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/alerts.db"
    cfg.api_keys_raw = "k1"
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.dashboard_admin_password = SecretStr("hunter2")
    cfg.dashboard_session_secret = SecretStr("x" * 32)
    cfg.grace_period_s = 1
    cfg.default_timeout_s = 5
    # Plan 8 D8.7 — set rule + mapping via the JSON env path so the
    # lifespan's AlertRouter is constructed with the exact same
    # values an operator would supply through the environment.
    cfg.alert_rules_json = (
        '{"fail":["always"],'
        '"cancel":["timeout"],'
        '"complete":["tag=notify"]}'
    )
    cfg.feishu_user_mapping_raw = "alice=ou_alice_open_id"
    return cfg


@pytest_asyncio.fixture
async def app_and_backend(tmp_path: Path):
    """Boot the FastAPI app + swap a spy backend onto the wired
    AlertRouter so the test asserts on actual `send_card` calls
    without needing real Feishu credentials.

    The store is exposed for seeding sessions whose ``owner`` /
    ``tags`` the FailureSubscriber will resolve when a terminal
    event is published.
    """
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    store = SessionRepository(eng)
    # The bus / router live on the app instance after the lifespan
    # opens; seed the store BEFORE we hand control to the lifespan
    # so the rows exist by the time terminal events arrive.
    await store.create_session(
        id="sid-failed",
        spec_json={"prompt": "boom"},
        trace_id=None,
        backend="inprocess",
        tags=(),
        owner="alice",
    )
    await store.create_session(
        id="sid-cancel-user",
        spec_json={"prompt": "stopme"},
        trace_id=None,
        backend="inprocess",
        tags=(),
        owner="alice",
    )
    await store.create_session(
        id="sid-complete-notify",
        spec_json={"prompt": "ok"},
        trace_id=None,
        backend="inprocess",
        tags=("notify",),
        owner="alice",
    )
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as ac, app.router.lifespan_context(app):
        # Inject the spy backend into the running router. The
        # lifespan constructed an AlertRouter pointing at a (likely
        # None) Feishu backend; swap both backend + card_builder
        # references so the rules + cooldown + mention pipeline
        # routes into the spy under test instead.
        spy = _SpyBackend()
        from gg_relay.im.backends.feishu import FeishuCardBuilder

        router = app.state.alert_router
        router._backend = spy
        router._card_builder = FeishuCardBuilder()
        yield ac, app, spy


async def _publish_and_wait(app, event: SessionStateChanged) -> None:
    """Publish on the live bus and yield enough event-loop turns
    for the FailureSubscriber consumer task to drain the message,
    invoke the router, and (on a match) call ``send_card`` on the
    spy backend.

    The subscriber drains one item per ``await`` so a single
    ``asyncio.sleep(0)`` would race with the consumer's loop body;
    a tiny real sleep gives the consumer task a deterministic
    window to land the dispatch."""
    await app.state.bus.publish(event)
    for _ in range(5):
        await asyncio.sleep(0.01)


class TestFailedSession:
    async def test_failed_session_triggers_alert_card(
        self, app_and_backend
    ) -> None:
        ac, app, spy = app_and_backend
        del ac
        event = SessionStateChanged(
            session_id="sid-failed",
            from_state="running",
            to_state="failed",
            reason="http:502",
        )
        await _publish_and_wait(app, event)
        assert len(spy.sent) == 1
        card = spy.sent[0]
        assert card.metadata["alert_event_type"] == "session_failed"
        # Owner ``alice`` is in the configured mapping → mention resolved.
        assert card.metadata["mention_open_id"] == "ou_alice_open_id"
        assert card.payload["header"]["template"] == "red"


class TestUserCancelFilter:
    async def test_user_cancel_no_alert(self, app_and_backend) -> None:
        ac, app, spy = app_and_backend
        del ac
        event = SessionStateChanged(
            session_id="sid-cancel-user",
            from_state="running",
            to_state="cancelled",
            reason="user_request",
        )
        await _publish_and_wait(app, event)
        assert spy.sent == []


class TestCompleteTagNotify:
    async def test_complete_tag_notify_alert(self, app_and_backend) -> None:
        ac, app, spy = app_and_backend
        del ac
        event = SessionStateChanged(
            session_id="sid-complete-notify",
            from_state="running",
            to_state="completed",
            reason=None,
        )
        await _publish_and_wait(app, event)
        assert len(spy.sent) == 1
        card = spy.sent[0]
        assert card.metadata["alert_event_type"] == "session_completed"
        assert card.payload["header"]["template"] == "green"
        # ``tags=("notify",)`` round-trips through store → subscriber
        # → router rule match.
        body = next(
            el["content"]
            for el in card.payload["elements"]
            if el.get("tag") == "markdown"
        )
        assert "sid-complete-notify" in body
