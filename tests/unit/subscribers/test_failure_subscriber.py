"""FailureSubscriber filter tests — Plan 8 Task 11 (D8.7).

Drives the subscriber directly via :meth:`FailureSubscriber.handle` so
we exercise the filter logic without spinning a bus + consumer task
pair. The router is a tiny recording stub so each assertion checks
the exact ``dispatch`` payload the router would have received.

The bus-side type is :class:`SessionStateChanged` (frozen + slots), so
we can't attach extra attributes for owner/tags. We test the filter
logic with a duck-typed :class:`_StubEvent` that exposes the four
fields the subscriber reads + the optional owner/tags inlined.
:class:`_RealEventTest` covers the genuine
:class:`SessionStateChanged` path through the store-fallback branch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from gg_relay.core import SessionStateChanged
from gg_relay.subscribers.failure_subscriber import FailureSubscriber

pytestmark = pytest.mark.asyncio


@dataclass
class _RecordingRouter:
    """Stand-in for AlertRouter that records every dispatch call."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    return_value: bool = True

    async def dispatch(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return self.return_value


@dataclass
class _StubEvent:
    """Duck-typed terminal event for unit testing the subscriber.

    Mirrors :class:`SessionStateChanged`'s read surface
    (``session_id`` / ``to_state`` / ``reason``) plus the optional
    ``owner`` / ``tags`` attributes the subscriber reads via
    :func:`getattr`. Plain dataclass (no slots) so tests can attach
    extra metadata inline without monkey-patching."""

    session_id: str
    to_state: str
    reason: str | None = "operational"
    owner: str | None = None
    tags: list[str] = field(default_factory=list)


def _make_event(
    *,
    sid: str = "s1",
    to_state: str,
    reason: str | None = "operational",
    owner: str | None = None,
    tags: list[str] | None = None,
) -> _StubEvent:
    return _StubEvent(
        session_id=sid,
        to_state=to_state,
        reason=reason,
        owner=owner,
        tags=list(tags) if tags is not None else [],
    )


class TestTerminalDispatch:
    async def test_session_failed_always_dispatched(self) -> None:
        router = _RecordingRouter()
        sub = FailureSubscriber(bus=None, alert_router=router)  # type: ignore[arg-type]

        event = _make_event(
            sid="sid-fail-1",
            to_state="failed",
            reason="http:502",
            owner="alice",
            tags=["billing"],
        )
        dispatched = await sub.handle(event)

        assert dispatched is True
        assert len(router.calls) == 1
        call = router.calls[0]
        assert call["event_type"] == "session_failed"
        assert call["session_id"] == "sid-fail-1"
        assert call["owner"] == "alice"
        assert call["tags"] == ["billing"]
        assert call["end_reason"] == "http:502"
        assert call["event"] is event

    async def test_session_completed_forwarded_to_router(self) -> None:
        """Subscriber doesn't gate completed events — that decision
        belongs to the router's rule list. We just verify the
        forwarding path."""
        router = _RecordingRouter()
        sub = FailureSubscriber(bus=None, alert_router=router)  # type: ignore[arg-type]

        event = _make_event(
            sid="sid-ok-1",
            to_state="completed",
            reason=None,
            tags=["notify"],
        )
        await sub.handle(event)
        assert len(router.calls) == 1
        assert router.calls[0]["event_type"] == "session_completed"
        # ``reason=None`` collapses to the literal "unknown" so the
        # cooldown key stays stable across cancel/complete reruns.
        assert router.calls[0]["end_reason"] == "unknown"

    async def test_non_terminal_state_change_ignored(self) -> None:
        """``running → paused`` etc. must NOT reach the router."""
        router = _RecordingRouter()
        sub = FailureSubscriber(bus=None, alert_router=router)  # type: ignore[arg-type]

        event = _make_event(to_state="paused", reason="operator")
        dispatched = await sub.handle(event)

        assert dispatched is False
        assert router.calls == []


class TestUserCancelFilter:
    @pytest.mark.parametrize("reason", ["user_cancel", "user_request"])
    async def test_user_initiated_cancel_not_dispatched(
        self, reason: str
    ) -> None:
        """Operator-initiated cancels are never alerts — the human is
        already aware of the action they just took."""
        router = _RecordingRouter()
        sub = FailureSubscriber(bus=None, alert_router=router)  # type: ignore[arg-type]

        event = _make_event(to_state="cancelled", reason=reason)
        dispatched = await sub.handle(event)

        assert dispatched is False
        assert router.calls == []

    async def test_operational_cancel_still_dispatched(self) -> None:
        """``end_reason="timeout"`` is operational, not user-initiated
        → MUST reach the router (which then applies its cancel rule)."""
        router = _RecordingRouter()
        sub = FailureSubscriber(bus=None, alert_router=router)  # type: ignore[arg-type]

        event = _make_event(to_state="cancelled", reason="timeout")
        dispatched = await sub.handle(event)

        assert dispatched is True
        assert router.calls[0]["event_type"] == "session_cancelled"
        assert router.calls[0]["end_reason"] == "timeout"


class TestStoreFallback:
    """When the event lacks owner/tags attributes (the production
    :class:`SessionStateChanged` is ``slots=True`` and exposes
    neither), the subscriber looks them up in the store. Tests here
    use the genuine typed event so the resolution branch exercises
    the actual ``hasattr`` check the subscriber performs."""

    async def test_store_lookup_supplies_owner_and_tags(self) -> None:
        recorded: list[str] = []

        @dataclass
        class _StubStore:
            async def get_session(self, sid: str) -> dict[str, Any]:
                recorded.append(sid)
                return {"owner": "bob", "tags": ["notify", "billing"]}

        router = _RecordingRouter()
        sub = FailureSubscriber(
            bus=None,  # type: ignore[arg-type]
            alert_router=router,
            store=_StubStore(),
        )

        event = SessionStateChanged(
            session_id="sid-store-1",
            from_state="running",
            to_state="failed",
            reason="boom",
        )
        await sub.handle(event)

        assert recorded == ["sid-store-1"]
        call = router.calls[0]
        assert call["owner"] == "bob"
        assert call["tags"] == ["notify", "billing"]

    async def test_store_miss_falls_back_to_anonymous(self) -> None:
        @dataclass
        class _EmptyStore:
            async def get_session(self, sid: str) -> dict[str, Any] | None:
                del sid
                return None

        router = _RecordingRouter()
        sub = FailureSubscriber(
            bus=None,  # type: ignore[arg-type]
            alert_router=router,
            store=_EmptyStore(),
        )

        event = SessionStateChanged(
            session_id="sid-miss",
            from_state="running",
            to_state="failed",
            reason="boom",
        )
        await sub.handle(event)
        assert router.calls[0]["owner"] is None
        assert router.calls[0]["tags"] == []

    async def test_no_store_no_attrs_alerts_anonymously(self) -> None:
        """No store + no inline attrs → subscriber alerts with empty
        metadata. The router still applies its rule list against
        the empty tags / unknown owner; ``fail`` rule defaults to
        ``always`` so a failed session would still alert."""
        router = _RecordingRouter()
        sub = FailureSubscriber(
            bus=None, alert_router=router, store=None  # type: ignore[arg-type]
        )
        event = SessionStateChanged(
            session_id="sid-bare",
            from_state="running",
            to_state="failed",
            reason="boom",
        )
        await sub.handle(event)
        assert router.calls[0]["owner"] is None
        assert router.calls[0]["tags"] == []
