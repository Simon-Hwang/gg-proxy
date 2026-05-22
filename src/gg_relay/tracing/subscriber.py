"""EventBus → OTel span bridge (Plan 5 D5.2=A3 typed-event consumer).

Subscribes to three typed event classes:

- :class:`SessionStateChanged` — first ``running`` transition opens the
  per-session span; the matching terminal (``completed`` / ``failed`` /
  ``cancelled`` / ``interrupted``) closes it.
- :class:`ToolRequested` / :class:`ToolResolved` — open / close per-tool
  child spans under the session.
- :class:`InstallError` — surface as a span event on the session span.

A small str-topic fallback (``frame`` topic with dict payloads) is kept
for forward-compat with wire-frame variants that don't yet have typed
counterparts; it can be removed once Plan 6 fully typed-only.

This module stays framework-light so the same subscriber can be plugged
into an in-memory exporter (tests) or an OTLP exporter (prod).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span

from gg_relay.core import (
    EventBus,
    InstallError,
    SessionStateChanged,
    ToolRequested,
    ToolResolved,
)

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
            async for event in self._bus.subscribe(SessionStateChanged):
                self._on_state(event)

        async def _tool_request_task() -> None:
            async for event in self._bus.subscribe(ToolRequested):
                self._on_tool_request(event)

        async def _tool_result_task() -> None:
            async for event in self._bus.subscribe(ToolResolved):
                self._on_tool_result(event)

        async def _install_error_task() -> None:
            async for event in self._bus.subscribe(InstallError):
                self._on_install_error(event)

        async def _legacy_frame_task() -> None:
            # Forward-compat: wire frames not yet typed still arrive via
            # the str "frame" topic (SessionManager fallback path).
            async for event in self._bus.subscribe("frame"):
                self._on_legacy_frame(event)

        async def _stop_waiter() -> None:
            await self._stop.wait()

        tasks = [
            asyncio.create_task(_state_task(), name="otel-state"),
            asyncio.create_task(_tool_request_task(), name="otel-tool-req"),
            asyncio.create_task(_tool_result_task(), name="otel-tool-res"),
            asyncio.create_task(_install_error_task(), name="otel-install-err"),
            asyncio.create_task(_legacy_frame_task(), name="otel-legacy"),
            asyncio.create_task(_stop_waiter(), name="otel-stop"),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            for span in list(self._tools.values()):
                with contextlib.suppress(Exception):
                    span.end()
            self._tools.clear()
            for span in list(self._sessions.values()):
                with contextlib.suppress(Exception):
                    span.end()
            self._sessions.clear()

    def _on_state(self, event: SessionStateChanged) -> None:
        sid = event.session_id
        to_state = event.to_state
        if not sid or not to_state:
            return
        if to_state == "running" and sid not in self._sessions:
            self._sessions[sid] = self._tracer.start_span(
                f"session:{sid}", attributes={"gg_relay.session_id": sid}
            )
        elif to_state in _TERMINAL_STATES:
            span = self._sessions.pop(sid, None)
            if span is not None:
                span.set_attribute("gg_relay.end_status", to_state)
                if event.reason:
                    span.set_attribute("gg_relay.end_reason", event.reason)
                span.end()

    def _on_tool_request(self, event: ToolRequested) -> None:
        if not event.req_id:
            return
        parent = self._sessions.get(event.session_id)
        ctx = trace.set_span_in_context(parent) if parent else None
        self._tools[event.req_id] = self._tracer.start_span(
            f"tool:{event.tool}",
            context=ctx,
            attributes={
                "gg_relay.session_id": event.session_id,
                "gg_relay.req_id": event.req_id,
                "gg_relay.tool": event.tool,
            },
        )

    def _on_tool_result(self, event: ToolResolved) -> None:
        span = self._tools.pop(event.req_id, None)
        if span is None:
            return
        span.set_attribute(
            "gg_relay.tool_status", "ok" if event.ok else "error"
        )
        if event.error:
            span.set_attribute("gg_relay.tool_error", event.error[:512])
        span.end()

    def _on_install_error(self, event: InstallError) -> None:
        parent = self._sessions.get(event.session_id)
        if parent is None:
            return
        parent.add_event(
            "error",
            attributes={
                "code": event.code,
                "message": event.message[:512],
            },
        )

    def _on_legacy_frame(self, event: dict[str, Any]) -> None:
        """Handle the str-topic ``frame`` fallback (forward-compat path)."""
        sid = event.get("session_id")
        ftype = event.get("type")
        if not isinstance(sid, str) or not isinstance(ftype, str):
            return
        if ftype == "error":
            parent = self._sessions.get(sid)
            if parent is not None:
                parent.add_event(
                    "error",
                    attributes={
                        "code": str(event.get("code", "")),
                        "message": str(event.get("message", ""))[:512],
                    },
                )
