"""EventBus → OTel span bridge.

Subscribes to two topics:

- ``session_state`` carries lifecycle status changes (running, completed,
  failed, cancelled, interrupted). The first ``running`` event opens the
  session span; the terminal event closes it.
- ``frame`` carries per-frame events (msg.chunk, tool.request,
  tool.result, session.end, error). ``tool.request`` opens a child span
  under the session; the matching ``tool.result`` closes it.

This module is intentionally framework-light so the same subscriber can
be plugged into tests with an in-memory exporter and into production with
an OTLP exporter.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span

from gg_relay.core import EventBus

logger = logging.getLogger("gg_relay.tracing")

_TERMINAL_STATES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)


class OtelSubscriber:
    """Background task that drains the EventBus and emits spans.

    Usage::

        sub = OtelSubscriber(bus, provider)
        task = asyncio.create_task(sub.run())
        ...
        await sub.stop(); await task
    """

    def __init__(
        self,
        bus: EventBus,
        provider: TracerProvider,
        *,
        tracer_name: str = "gg_relay.session",
    ) -> None:
        self._bus = bus
        self._tracer = provider.get_tracer(tracer_name)
        self._sessions: dict[str, Span] = {}
        self._tools: dict[str, Span] = {}
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        async def _state_task() -> None:
            async for event in self._bus.subscribe("session_state"):
                self._on_state(event)

        async def _frame_task() -> None:
            async for event in self._bus.subscribe("frame"):
                self._on_frame(event)

        async def _stop_waiter() -> None:
            await self._stop.wait()

        tasks = [
            asyncio.create_task(_state_task(), name="otel-state"),
            asyncio.create_task(_frame_task(), name="otel-frame"),
            asyncio.create_task(_stop_waiter(), name="otel-stop"),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            # End any spans we still own so the exporter sees them.
            for span in list(self._tools.values()):
                with contextlib.suppress(Exception):
                    span.end()
            self._tools.clear()
            for span in list(self._sessions.values()):
                with contextlib.suppress(Exception):
                    span.end()
            self._sessions.clear()

    def _on_state(self, event: dict[str, Any]) -> None:
        sid = event.get("session_id")
        status = event.get("status")
        if not isinstance(sid, str) or not isinstance(status, str):
            return
        if status == "running" and sid not in self._sessions:
            self._sessions[sid] = self._tracer.start_span(
                f"session:{sid}", attributes={"gg_relay.session_id": sid}
            )
        elif status in _TERMINAL_STATES:
            span = self._sessions.pop(sid, None)
            if span is not None:
                span.set_attribute("gg_relay.end_status", status)
                if event.get("reason"):
                    span.set_attribute(
                        "gg_relay.end_reason", str(event["reason"])
                    )
                span.end()

    def _on_frame(self, event: dict[str, Any]) -> None:
        sid = event.get("session_id")
        ftype = event.get("type")
        if not isinstance(sid, str) or not isinstance(ftype, str):
            return
        parent = self._sessions.get(sid)
        if ftype == "tool.request":
            req_id = str(event.get("req_id", ""))
            tool = str(event.get("tool", ""))
            ctx = trace.set_span_in_context(parent) if parent else None
            self._tools[req_id] = self._tracer.start_span(
                f"tool:{tool}",
                context=ctx,
                attributes={
                    "gg_relay.session_id": sid,
                    "gg_relay.req_id": req_id,
                    "gg_relay.tool": tool,
                },
            )
        elif ftype == "tool.result":
            req_id = str(event.get("req_id", ""))
            span = self._tools.pop(req_id, None)
            if span is not None:
                span.set_attribute(
                    "gg_relay.tool_status", str(event.get("status", ""))
                )
                span.end()
        elif ftype == "error":
            if parent is not None:
                parent.add_event(
                    "error",
                    attributes={
                        "code": str(event.get("code", "")),
                        "message": str(event.get("message", ""))[:512],
                    },
                )
