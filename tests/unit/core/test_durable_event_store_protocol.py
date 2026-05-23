"""DurableEventStore Protocol conformance — Plan 7 Task 13 (D7.17).

Validates that the runtime-checkable Protocol does what its docstring
promises: real implementations match, half-implementations don't. The
import path runs through ``gg_relay.core`` to make sure the Protocol is
re-exported at the public surface.
"""
from __future__ import annotations

from gg_relay.core import DurableEventStore
from gg_relay.store.durable_event import (
    InMemoryDurableEventStore,
    SqlAlchemyDurableEventStore,
)


class TestDurableEventStoreProtocol:
    def test_in_memory_store_implements_protocol(self) -> None:
        """The test-only InMemory store satisfies the runtime check."""
        store = InMemoryDurableEventStore()
        assert isinstance(store, DurableEventStore)

    def test_sqla_store_implements_protocol(self) -> None:
        """``SqlAlchemyDurableEventStore`` matches the protocol surface.

        We don't need a live engine for ``isinstance`` — runtime
        protocol checks inspect attribute presence on the instance, so
        a ``None`` engine sentinel is enough to construct.
        """
        store = SqlAlchemyDurableEventStore(engine=None)  # type: ignore[arg-type]
        assert isinstance(store, DurableEventStore)

    def test_partial_implementation_rejected(self) -> None:
        """An object missing ``fetch_after`` must NOT satisfy the protocol."""

        class _Half:
            async def persist(self, event):  # type: ignore[no-untyped-def]
                return 1

        assert not isinstance(_Half(), DurableEventStore)
