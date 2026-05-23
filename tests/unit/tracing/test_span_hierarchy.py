"""Plan 7 Task 15 (D7.9) — 3-tier span hierarchy tests for OtelSubscriber.

Covers pause/resume run-span splitting, terminal finalize emission, fixed
``relay.tool_call`` span name (tool moved to attribute to prevent
high-cardinality span tables), and the Plan-7-transition double-write
attributes (``session.id`` + ``gg_relay.session_id``; ``gen_ai.tool.name``
+ ``gg_relay.tool``).
"""
from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from gg_relay.core import EventBus, SessionStateChanged, ToolRequested
from gg_relay.tracing.subscriber import OtelSubscriber


@pytest.fixture
async def harness():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    bus = EventBus()
    sub = OtelSubscriber(bus, provider)
    task = asyncio.create_task(sub.run())
    await asyncio.sleep(0.01)
    try:
        yield bus, sub, exporter
    finally:
        await sub.stop()
        await bus.close()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (TimeoutError, Exception):  # noqa: BLE001
            task.cancel()


def _names(spans) -> list[str]:
    return [s.name for s in spans]


def _filter(spans, name: str, sid: str | None = None):
    return [
        s for s in spans
        if s.name == name
        and (sid is None or (
            s.attributes is not None
            and s.attributes.get("session.id") == sid
        ))
    ]


# ── 1. RUNNING creates root + run ─────────────────────────────────────


class TestRootAndRun:
    async def test_running_creates_root_and_run(self, harness):
        bus, sub, _exporter = harness
        await bus.publish(
            SessionStateChanged(
                session_id="h1", from_state="queued", to_state="running"
            )
        )
        await asyncio.sleep(0.05)
        # Both spans are open and tracked in the subscriber's dicts.
        assert "h1" in sub._roots
        assert "h1" in sub._runs

    async def test_root_attributes_include_session_id_double_write(
        self, harness
    ):
        bus, _sub, exporter = harness
        await bus.publish(
            SessionStateChanged(
                session_id="h-attr",
                from_state="queued",
                to_state="running",
            )
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            SessionStateChanged(
                session_id="h-attr",
                from_state="running",
                to_state="completed",
            )
        )
        await asyncio.sleep(0.05)
        spans = list(exporter.get_finished_spans())
        roots = _filter(spans, "relay.session", "h-attr")
        assert len(roots) == 1
        attrs = roots[0].attributes or {}
        # Plan 7 transition: both keys carry the same id.
        assert attrs.get("session.id") == "h-attr"
        assert attrs.get("gg_relay.session_id") == "h-attr"


# ── 2. PAUSE / RESUME ────────────────────────────────────────────────


class TestPauseResume:
    async def test_pause_ends_run_root_open(self, harness):
        bus, sub, exporter = harness
        await bus.publish(
            SessionStateChanged(
                session_id="p1", from_state="queued", to_state="running"
            )
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            SessionStateChanged(
                session_id="p1", from_state="running", to_state="paused"
            )
        )
        await asyncio.sleep(0.05)
        # Run is gone from active dict, root remains.
        assert "p1" not in sub._runs
        assert "p1" in sub._roots
        # The closed run carries end_reason="paused".
        spans = list(exporter.get_finished_spans())
        runs = _filter(spans, "relay.session.run", "p1")
        assert len(runs) == 1
        run_attrs = runs[0].attributes or {}
        assert run_attrs.get("end_reason") == "paused"
        # Root span is still open → not exported yet.
        roots = _filter(spans, "relay.session", "p1")
        assert roots == []

    async def test_resume_creates_new_run_reuses_root(self, harness):
        bus, sub, exporter = harness
        await bus.publish(
            SessionStateChanged(
                session_id="r1", from_state="queued", to_state="running"
            )
        )
        await asyncio.sleep(0.05)
        root_span_before = sub._roots["r1"]
        first_run_ctx = sub._runs["r1"].context

        await bus.publish(
            SessionStateChanged(
                session_id="r1", from_state="running", to_state="paused"
            )
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            SessionStateChanged(
                session_id="r1", from_state="paused", to_state="running"
            )
        )
        await asyncio.sleep(0.05)
        # Root span is the SAME instance; a fresh run was opened.
        assert sub._roots["r1"] is root_span_before
        assert "r1" in sub._runs
        assert sub._runs["r1"].context.span_id != first_run_ctx.span_id

        # Finalize the session so we can inspect the run count downstream.
        await bus.publish(
            SessionStateChanged(
                session_id="r1",
                from_state="running",
                to_state="completed",
            )
        )
        await asyncio.sleep(0.05)
        spans = list(exporter.get_finished_spans())
        # Two distinct run spans for the same session (one per active segment).
        runs = _filter(spans, "relay.session.run", "r1")
        assert len(runs) == 2


# ── 3. TERMINAL → run + finalize + root all closed ───────────────────


class TestTermination:
    async def test_completed_ends_run_finalize_root(self, harness):
        bus, sub, exporter = harness
        await bus.publish(
            SessionStateChanged(
                session_id="t1", from_state="queued", to_state="running"
            )
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            SessionStateChanged(
                session_id="t1",
                from_state="running",
                to_state="completed",
            )
        )
        await asyncio.sleep(0.05)
        # All bookkeeping cleared.
        assert "t1" not in sub._roots
        assert "t1" not in sub._runs
        spans = list(exporter.get_finished_spans())
        # All three spans are exported.
        assert _filter(spans, "relay.session", "t1")
        assert _filter(spans, "relay.session.run", "t1")
        assert _filter(spans, "relay.session.finalize", "t1")
        # Finalize carries end_status (stable attr name dashboards can query).
        final = _filter(spans, "relay.session.finalize", "t1")[0]
        assert (final.attributes or {}).get("end_status") == "completed"


# ── 4. tool_call fixed span name + tool attr ─────────────────────────


class TestToolSpan:
    async def test_tool_call_fixed_name_tool_attr(self, harness):
        bus, _sub, exporter = harness
        await bus.publish(
            SessionStateChanged(
                session_id="tc1", from_state="queued", to_state="running"
            )
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            ToolRequested(
                session_id="tc1",
                seq=1,
                req_id="tc1:r0",
                tool="Bash",
                args_redacted={},
            )
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            SessionStateChanged(
                session_id="tc1",
                from_state="running",
                to_state="completed",
            )
        )
        await asyncio.sleep(0.05)
        spans = list(exporter.get_finished_spans())
        # Fixed span name; tool name does NOT appear in span names.
        assert "relay.tool_call" in _names(spans)
        assert "tool:Bash" not in _names(spans)
        tool_spans = [s for s in spans if s.name == "relay.tool_call"]
        assert len(tool_spans) == 1
        attrs = tool_spans[0].attributes or {}
        # Double-write attr keys.
        assert attrs.get("gg_relay.tool") == "Bash"
        assert attrs.get("gen_ai.tool.name") == "Bash"


# ── 5/6/7. double-write coverage (explicit, single-purpose) ──────────


class TestDoubleWrite:
    async def test_double_write_session_id_attr(self, harness):
        bus, _sub, exporter = harness
        await bus.publish(
            SessionStateChanged(
                session_id="dw1", from_state="queued", to_state="running"
            )
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            SessionStateChanged(
                session_id="dw1",
                from_state="running",
                to_state="completed",
            )
        )
        await asyncio.sleep(0.05)
        spans = list(exporter.get_finished_spans())
        for name in ("relay.session", "relay.session.run", "relay.session.finalize"):
            matched = _filter(spans, name, "dw1")
            assert matched, f"missing {name}"
            attrs = matched[0].attributes or {}
            assert attrs.get("session.id") == "dw1", name
            assert attrs.get("gg_relay.session_id") == "dw1", name

    async def test_double_write_tool_attr(self, harness):
        bus, _sub, exporter = harness
        await bus.publish(
            SessionStateChanged(
                session_id="dw2", from_state="queued", to_state="running"
            )
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            ToolRequested(
                session_id="dw2",
                seq=1,
                req_id="dw2:r0",
                tool="Read",
                args_redacted={},
            )
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            SessionStateChanged(
                session_id="dw2",
                from_state="running",
                to_state="completed",
            )
        )
        await asyncio.sleep(0.05)
        spans = list(exporter.get_finished_spans())
        tool = next(s for s in spans if s.name == "relay.tool_call")
        attrs = tool.attributes or {}
        # Both attribute keys point at the same tool name.
        assert attrs.get("gg_relay.tool") == "Read"
        assert attrs.get("gen_ai.tool.name") == "Read"
