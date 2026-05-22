"""OpenTelemetry integration."""
from gg_relay.tracing.setup import setup_tracer
from gg_relay.tracing.subscriber import OtelSubscriber

__all__ = ["OtelSubscriber", "setup_tracer"]
