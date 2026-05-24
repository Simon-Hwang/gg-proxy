"""Plan 8 D8.7 — terminal-event subscribers (alert routing).

The subscribers in this package read typed :class:`RelayEvent` traffic
off :class:`gg_relay.core.event_bus.EventBus` and translate matched
events into IM-backed alerts. They're intentionally decoupled from the
:mod:`gg_relay.im` subscriber (which renders every typed event into a
card) — this package only fires when an *alert rule* matches, which
keeps the noisy channels quiet while still surfacing real incidents.

Wiring is two-step (see ``gg_relay.api.main.lifespan``):

1. Construct an :class:`AlertRouter` with the parsed
   ``cfg.alert_rules`` / ``cfg.feishu_user_mapping`` plus an
   :class:`gg_relay.im.protocol.IMBackend` for transport.
2. Wrap it in a :class:`FailureSubscriber` and call ``start(bus)``
   to spawn the consumer task; ``await failure_sub.stop()`` in the
   lifespan ``finally`` drains it gracefully.
"""
from gg_relay.subscribers.alert_router import AlertRouter
from gg_relay.subscribers.cost_metric_subscriber import CostMetricSubscriber
from gg_relay.subscribers.failure_subscriber import FailureSubscriber

__all__ = ["AlertRouter", "CostMetricSubscriber", "FailureSubscriber"]
