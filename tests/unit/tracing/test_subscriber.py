"""Unit tests for :class:`OtelSubscriber` and :func:`setup_tracer`.

Plan 5 D5.2=A3 migration: publishes typed events (SessionStateChanged,
ToolRequested, ToolResolved, InstallError) instead of the legacy dict
"session_state" / "frame" topics; the str-topic forward-compat path is
exercised by ``test_error_frame_legacy_path`` below.

Uses :class:`InMemorySpanExporter` so spans can be asserted directly.
"""
from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from gg_relay.core import (
    EventBus,
    InstallError,
    SessionStateChanged,
    ToolRequested,
    ToolResolved,
)
from gg_relay.tracing.setup import setup_tracer
from gg_relay.tracing.subscriber import OtelSubscriber


@pytest.fixture
def provider_and_exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


@pytest.fixture
async def harness(provider_and_exporter):
    provider, exporter = provider_and_exporter
    bus = EventBus()
    sub = OtelSubscriber(bus, provider)
    run_task = asyncio.create_task(sub.run())
    await asyncio.sleep(0.01)
    try:
        yield bus, sub, exporter
    finally:
        await sub.stop()
        await bus.close()
        try:
            await asyncio.wait_for(run_task, timeout=1.0)
        except (TimeoutError, Exception):  # noqa: BLE001
            run_task.cancel()


class TestSetupTracer:
    def test_console_exporter_returns_provider(self):
        provider = setup_tracer(exporter="console", install_global=False)
        assert isinstance(provider, TracerProvider)

    def test_unknown_exporter_raises(self):
        with pytest.raises(ValueError, match="unknown exporter"):
            setup_tracer(exporter="redis", install_global=False)  # type: ignore[arg-type]


class TestSubscriberSpans:
    async def test_session_start_then_end_emits_session_span(self, harness):
        bus, _sub, exporter = harness
        await bus.publish(
            SessionStateChanged(
                session_id="s1", from_state="queued", to_state="running"
            )
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            SessionStateChanged(
                session_id="s1",
                from_state="running",
                to_state="completed",
            )
        )
        await asyncio.sleep(0.05)
        spans = list(exporter.get_finished_spans())
        names = [s.name for s in spans]
        assert "session:s1" in names
        sess = next(s for s in spans if s.name == "session:s1")
        assert sess.attributes is not None
        assert sess.attributes.get("gg_relay.end_status") == "completed"

    async def test_tool_request_creates_child_span_under_session(self, harness):
        bus, _sub, exporter = harness
        await bus.publish(
            SessionStateChanged(
                session_id="s2", from_state="queued", to_state="running"
            )
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            ToolRequested(
                session_id="s2",
                seq=1,
                req_id="s2:r0",
                tool="WriteFile",
                args_redacted={"path": "/tmp/x"},
            )
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            ToolResolved(
                session_id="s2",
                seq=2,
                req_id="s2:r0",
                ok=True,
                result_redacted={"bytes": 4},
            )
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            SessionStateChanged(
                session_id="s2", from_state="running", to_state="completed"
            )
        )
        await asyncio.sleep(0.05)
        spans = list(exporter.get_finished_spans())
        names = [s.name for s in spans]
        assert "session:s2" in names
        assert "tool:WriteFile" in names
        sess = next(s for s in spans if s.name == "session:s2")
        tool = next(s for s in spans if s.name == "tool:WriteFile")
        assert tool.parent is not None
        assert tool.parent.trace_id == sess.context.trace_id
        assert tool.parent.span_id == sess.context.span_id

    async def test_install_error_event_lands_on_session_span(self, harness):
        bus, _sub, exporter = harness
        await bus.publish(
            SessionStateChanged(
                session_id="s3", from_state="queued", to_state="running"
            )
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            InstallError(session_id="s3", code="boom", message="oops")
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            SessionStateChanged(
                session_id="s3",
                from_state="running",
                to_state="failed",
                reason="boom",
            )
        )
        await asyncio.sleep(0.05)
        spans = list(exporter.get_finished_spans())
        sess = next(s for s in spans if s.name == "session:s3")
        ev_names = [e.name for e in sess.events]
        assert "error" in ev_names

    async def test_error_frame_legacy_path(self, harness):
        """Forward-compat: unknown wire frames still arrive via str topic."""
        bus, _sub, exporter = harness
        await bus.publish(
            SessionStateChanged(
                session_id="s3legacy",
                from_state="queued",
                to_state="running",
            )
        )
        await asyncio.sleep(0.05)
        # legacy 2-arg form
        await bus.publish(
            "frame",
            {
                "session_id": "s3legacy",
                "type": "error",
                "code": "legacy",
                "message": "legacy-msg",
            },
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            SessionStateChanged(
                session_id="s3legacy",
                from_state="running",
                to_state="failed",
                reason="legacy",
            )
        )
        await asyncio.sleep(0.05)
        spans = list(exporter.get_finished_spans())
        sess = next(s for s in spans if s.name == "session:s3legacy")
        ev_names = [e.name for e in sess.events]
        assert "error" in ev_names

    async def test_subscriber_idempotent_running_event(self, harness):
        bus, _sub, exporter = harness
        await bus.publish(
            SessionStateChanged(
                session_id="s4", from_state="queued", to_state="running"
            )
        )
        await bus.publish(
            SessionStateChanged(
                session_id="s4", from_state="running", to_state="running"
            )
        )
        await bus.publish(
            SessionStateChanged(
                session_id="s4", from_state="running", to_state="completed"
            )
        )
        await asyncio.sleep(0.05)
        spans = list(exporter.get_finished_spans())
        assert sum(1 for s in spans if s.name == "session:s4") == 1
