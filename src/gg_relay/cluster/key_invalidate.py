"""Plan 9 D9.10 — KeyInvalidateSubscriber.

Cross-worker subscriber that listens for
:class:`gg_relay.core.events.KeyInvalidated` broadcasts and refreshes
``app.state.dashboard_internal_keys`` from the DB on receipt.

End-to-end flow
~~~~~~~~~~~~~~~

1. Admin endpoint (or CLI) calls
   :meth:`gg_relay.store.dashboard_keys.DashboardKeyStore.rotate(user)`
   and receives the new raw_key.
2. Endpoint awaits ``bus.publish(KeyInvalidated(usernames=(user,)))``.
   The bus is either the in-process :class:`EventBus` (single-worker)
   or :class:`RedisStreamEventBus` (multi-worker) — both deliver to
   every subscriber on every worker.
3. Each worker's :class:`KeyInvalidateSubscriber` task receives the
   event, calls :meth:`DashboardKeyStore.list_all`, and rewrites
   ``app.state.dashboard_internal_keys`` with the fresh mapping.
4. The next inbound request that triggers
   :class:`DashboardCookieMiddleware` reads the new key.

Why refresh the *full* mapping (not just the affected username)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A bulk rotation (operator rebuilds the dashboard_users config) can
race with KeyInvalidated events from previous rotations. Always
reloading the full mapping makes the result eventually-consistent
regardless of event ordering — at the cost of one extra SELECT per
broadcast, which is bounded by the dashboard user count (typically
< 20) and only fires on rotation, not on every request.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from gg_relay.core.events import KeyInvalidated

if TYPE_CHECKING:
    from starlette.applications import Starlette

    from gg_relay.core.protocol import EventBusBackend
    from gg_relay.store.dashboard_keys import DashboardKeyStore

logger = logging.getLogger("gg_relay.cluster.key_invalidate")


class KeyInvalidateSubscriber:
    """Background task that refreshes app.state on KeyInvalidated.

    Lifecycle:

    * :meth:`start` schedules the subscriber task on the running
      event loop. Idempotent — second call is a no-op while the
      task is running.
    * :meth:`stop` cancels the task and awaits its exit. Should be
      called from the lifespan shutdown handler BEFORE the bus
      closes so the subscriber doesn't wake up to a dead bus.
    """

    def __init__(
        self,
        *,
        bus: EventBusBackend,
        store: DashboardKeyStore,
        app: Starlette,
    ) -> None:
        self._bus = bus
        self._store = store
        self._app = app
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopped.clear()
            self._task = asyncio.create_task(
                self._run(), name="gg-relay.key-invalidate"
            )

    async def stop(self) -> None:
        self._stopped.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — defensive
                logger.exception("key_invalidate.shutdown_failed")
        self._task = None

    async def _run(self) -> None:
        """Subscribe to KeyInvalidated and refresh app.state on each."""
        sub = self._bus.subscribe(KeyInvalidated)
        try:
            async for event in sub:
                if self._stopped.is_set():
                    return
                # ReplayedEvent or the live class — both carry the
                # usernames tuple in payload / attribute.
                usernames = self._extract_usernames(event)
                try:
                    fresh = await self._store.list_all()
                except Exception:  # noqa: BLE001 — defensive
                    logger.exception(
                        "key_invalidate.reload_failed usernames=%s",
                        usernames,
                    )
                    continue
                self._app.state.dashboard_internal_keys = fresh
                logger.info(
                    "key_invalidate.refreshed usernames=%s mapping_size=%d",
                    usernames,
                    len(fresh),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — defensive
            logger.exception("key_invalidate.crashed")
        finally:
            with _suppress():
                await sub.aclose()  # type: ignore[attr-defined]

    @staticmethod
    def _extract_usernames(event: object) -> tuple[str, ...]:
        """Read the usernames tuple from a live or replayed event."""
        if isinstance(event, KeyInvalidated):
            return event.usernames
        # ReplayedEvent — usernames lives in payload dict
        payload = getattr(event, "payload", None)
        if isinstance(payload, dict):
            raw = payload.get("usernames", ())
            if isinstance(raw, (list, tuple)):
                return tuple(str(x) for x in raw)
        return ()


class _suppress:
    """Re-entrant ``contextlib.suppress(Exception)`` for the finally
    block. Avoids importing contextlib just for one suppression."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        return exc_type is not None and issubclass(exc_type, Exception)


__all__ = ["KeyInvalidateSubscriber"]
