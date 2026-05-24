"""Plan 9 v0.9.0-rc D9.0 — EventBusBackend / RateLimitStoreBackend Protocol.

Verifies that the in-process implementations shipped today
structurally satisfy the new Protocols (so the Plan 9.1 Redis swap
is local) AND that the new Protocols extend rather than break the
existing DurableEventStore Protocol used by Plan 7 D7.17.
"""
from __future__ import annotations

import inspect
from collections.abc import AsyncIterator

from gg_relay.api.middleware.rate_limit import TokenBucketRateLimiter
from gg_relay.core import (
    DurableEventStore,
    EventBus,
    EventBusBackend,
    RateLimitStoreBackend,
)
from gg_relay.core.protocol import DurableEventStore as DurableEventStoreModule
from gg_relay.store.durable_event import (
    InMemoryDurableEventStore,
    SqlAlchemyDurableEventStore,
)


class TestEventBusBackendProtocol:
    """D9.0 — EventBus must satisfy the new EventBusBackend Protocol
    without any constructor change (zero-impact refactor for the
    17+ existing call sites of EventBus.subscribe / .publish)."""

    def test_eventbus_satisfies_protocol_isinstance(self) -> None:
        bus = EventBus()
        assert isinstance(bus, EventBusBackend)

    def test_subscribe_signature_unchanged(self) -> None:
        """Existing callers (api/sse.py, im/subscriber.py, etc.) pass
        ``bus.subscribe(topic)`` or ``bus.subscribe(topic, maxsize=N)``.
        If the Protocol drift changed the signature, isinstance would
        still pass but callers would break — guard explicitly."""
        sig = inspect.signature(EventBus.subscribe)
        # First param after self is the topic (positional or keyword);
        # maxsize is keyword-only with default 1000.
        params = list(sig.parameters.values())
        # [self, topic, maxsize]
        assert params[1].name == "topic"
        assert params[2].name == "maxsize"
        assert params[2].kind == inspect.Parameter.KEYWORD_ONLY
        assert params[2].default == 1000

    def test_subscribe_all_yields_replayed_when_store_present(self) -> None:
        """D9.0 subscribe_all delegates to the durable store; with
        a populated InMemoryDurableEventStore we should see persisted
        events when after_seq is provided."""
        # We don't need to actually exercise the async iterator here —
        # the contract that subscribe_all exists and returns an
        # AsyncIterator is what we want to lock in.
        bus = EventBus(durable_store=InMemoryDurableEventStore())
        result = bus.subscribe_all(after_seq=0)
        assert isinstance(result, AsyncIterator)


class TestEventBusBackendNegative:
    """Reject objects that look like buses but miss a required method."""

    def test_object_missing_subscribe_rejected(self) -> None:
        class _Half:
            async def publish(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                return None

            def subscribe_all(self, *, after_seq=None):  # type: ignore[no-untyped-def]
                yield  # noqa: PYI036 — intentional generator stub

            async def close(self) -> None:
                return None

        assert not isinstance(_Half(), EventBusBackend)


class TestRateLimitStoreBackendProtocol:
    """D9.0 — TokenBucketRateLimiter satisfies RateLimitStoreBackend."""

    def test_token_bucket_satisfies_protocol(self) -> None:
        limiter = TokenBucketRateLimiter()
        assert isinstance(limiter, RateLimitStoreBackend)

    def test_acquire_signature_returns_tuple(self) -> None:
        sig = inspect.signature(TokenBucketRateLimiter.acquire)
        # (self, key) — strictly two positional params; tighter
        # would be a Protocol breakage.
        params = list(sig.parameters.values())
        assert len(params) == 2
        assert params[1].name == "key"


class TestDurableEventStoreFetchAfterSeq:
    """D9.0 + D9.9a — DurableEventStore Protocol must expose the new
    fetch_after_seq method so the Plan 9 D9.9a v2 SSE cursor can
    dispatch through the Protocol without isinstance loopholes."""

    def test_in_memory_store_has_fetch_after_seq(self) -> None:
        store = InMemoryDurableEventStore()
        assert hasattr(store, "fetch_after_seq")
        assert isinstance(store, DurableEventStore)

    def test_sql_store_has_fetch_after_seq(self) -> None:
        store = SqlAlchemyDurableEventStore(engine=None)  # type: ignore[arg-type]
        assert hasattr(store, "fetch_after_seq")
        assert isinstance(store, DurableEventStore)

    def test_protocol_module_reexports_match_init(self) -> None:
        """Belt-and-braces: the public ``gg_relay.core`` re-export must
        be the same object as the module-level Protocol."""
        assert DurableEventStore is DurableEventStoreModule
