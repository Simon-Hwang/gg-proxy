"""EventBus → OTel span bridge with 3-tier hierarchy (Plan 7 Task 15 / D7.9).

Span hierarchy (per session)::

    relay.session                  ← root, opens on first RUNNING transition
      relay.session.run            ← child, one per active run; ends on
      │                              PAUSED (end_reason="paused") and on
      │                              terminal; a fresh child is started on
      │                              every RESUME (reusing the same root)
      │   relay.tool_call          ← grandchild, fixed span name (tool
      │                              moved to ``gg_relay.tool`` /
      │                              ``gen_ai.tool.name`` attributes to
      │                              prevent high-cardinality span names)
      └── relay.session.finalize   ← short span emitted on terminal so
                                     dashboards can read end_status from a
                                     stable name without parsing the root

Double-write attributes (Plan 7 transition; 0.8 will drop the
``gg_relay.*`` aliases):

  * root / run :  ``session.id`` + ``gg_relay.session_id``
  * tool       :  ``gen_ai.tool.name`` + ``gg_relay.tool``

The legacy str-topic ``frame`` path is kept so wire frames that don't yet
have typed counterparts still surface as span events on the current run.

24h-limit note: PLAN §10 calls for a watchdog that force-ends root spans
older than 24h. Baseline implementation tracks each root's open time but
does **not** actively sweep — production deployments rotate the relay
process well within 24h; the bookkeeping is in place so a future task can
wire the sweep without re-shaping the subscriber.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span

from gg_relay.core import (
    EventBusBackend,
    InstallError,
    SessionStateChanged,
    ToolRequested,
    ToolResolved,
)

logger = logging.getLogger("gg_relay.tracing")

_TERMINAL_STATES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)
_RUNNING_STATE = "running"
_PAUSED_STATE = "paused"


class OtelSubscriber:
    """Background task that drains the EventBus and emits a 3-tier span tree.

    Usage::

        sub = OtelSubscriber(bus, provider)
        task = asyncio.create_task(sub.run())
        ...
        await sub.stop(); await task
    """

    def __init__(
        self,
        bus: EventBusBackend,
        provider: TracerProvider,
        *,
        tracer_name: str = "gg_relay.session",
    ) -> None:
        self._bus = bus
        self._tracer = provider.get_tracer(tracer_name)
        self._roots: dict[str, Span] = {}
        self._roots_started_at: dict[str, float] = {}
        self._runs: dict[str, Span] = {}
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
            for span in list(self._runs.values()):
                with contextlib.suppress(Exception):
                    span.end()
            self._runs.clear()
            for span in list(self._roots.values()):
                with contextlib.suppress(Exception):
                    span.end()
            self._roots.clear()
            self._roots_started_at.clear()

    # ── state machine → span tree ─────────────────────────────────────

    def _on_state(self, event: SessionStateChanged) -> None:
        sid = event.session_id
        to_state = event.to_state
        if not sid or not to_state:
            return
        if to_state == _RUNNING_STATE:
            self._open_or_resume_run(sid)
        elif to_state == _PAUSED_STATE:
            self._end_run(sid, end_reason="paused")
        elif to_state in _TERMINAL_STATES:
            self._terminate(sid, to_state, reason=event.reason)

    def _open_or_resume_run(self, sid: str) -> None:
        if sid not in self._roots:
            # First RUNNING for this session — open root + first run.
            root_attrs: dict[str, Any] = {
                "session.id": sid,
                "gg_relay.session_id": sid,  # double-write (Plan 7 transition)
            }
            root = self._tracer.start_span("relay.session", attributes=root_attrs)
            self._roots[sid] = root
            self._roots_started_at[sid] = time.monotonic()
        # Always start a fresh run span if there isn't one already open.
        # (Resume from PAUSED hits this branch — root stays, new run spawns.)
        if sid not in self._runs:
            root = self._roots[sid]
            ctx = trace.set_span_in_context(root)
            self._runs[sid] = self._tracer.start_span(
                "relay.session.run",
                context=ctx,
                attributes={
                    "session.id": sid,
                    "gg_relay.session_id": sid,
                },
            )

    def _end_run(self, sid: str, *, end_reason: str | None) -> None:
        run = self._runs.pop(sid, None)
        if run is None:
            return
        if end_reason:
            run.set_attribute("end_reason", end_reason)
            run.set_attribute("gg_relay.end_reason", end_reason)
        run.end()

    def _terminate(
        self, sid: str, to_state: str, *, reason: str | None
    ) -> None:
        # Force-end any tool spans still open for this session; otherwise a
        # crashed/forgotten ToolResolved would leak the grandchild span
        # past its parent root (which is itself about to close).
        for req_id, tool_span in list(self._tools.items()):
            attrs = getattr(tool_span, "attributes", None) or {}
            if attrs.get("session.id") == sid or attrs.get(
                "gg_relay.session_id"
            ) == sid:
                tool_span.set_attribute(
                    "gg_relay.tool_status", "unresolved"
                )
                tool_span.end()
                self._tools.pop(req_id, None)
        # End any open run first.
        run = self._runs.pop(sid, None)
        if run is not None:
            run.set_attribute("end_status", to_state)
            run.set_attribute("gg_relay.end_status", to_state)
            if reason:
                run.set_attribute("end_reason", reason)
                run.set_attribute("gg_relay.end_reason", reason)
            run.end()
        # Emit finalize span (short-lived) so dashboards can find a stable
        # span name carrying ``end_status``.
        root = self._roots.get(sid)
        if root is not None:
            ctx = trace.set_span_in_context(root)
            final = self._tracer.start_span(
                "relay.session.finalize",
                context=ctx,
                attributes={
                    "session.id": sid,
                    "gg_relay.session_id": sid,
                    "end_status": to_state,
                    "gg_relay.end_status": to_state,
                },
            )
            if reason:
                final.set_attribute("end_reason", reason)
                final.set_attribute("gg_relay.end_reason", reason)
            final.end()
        # Close root last so finalize is parented properly.
        root = self._roots.pop(sid, None)
        self._roots_started_at.pop(sid, None)
        if root is not None:
            root.set_attribute("end_status", to_state)
            root.set_attribute("gg_relay.end_status", to_state)
            if reason:
                root.set_attribute("end_reason", reason)
                root.set_attribute("gg_relay.end_reason", reason)
            root.end()

    # ── tool spans ────────────────────────────────────────────────────

    def _on_tool_request(self, event: ToolRequested) -> None:
        if not event.req_id:
            return
        parent = self._runs.get(event.session_id) or self._roots.get(
            event.session_id
        )
        ctx = trace.set_span_in_context(parent) if parent else None
        # Fixed span name (tool moved to attr) to prevent high-cardinality
        # span tables in the OTel backend.
        self._tools[event.req_id] = self._tracer.start_span(
            "relay.tool_call",
            context=ctx,
            attributes={
                "session.id": event.session_id,
                "gg_relay.session_id": event.session_id,
                "gg_relay.req_id": event.req_id,
                "gg_relay.tool": event.tool,
                "gen_ai.tool.name": event.tool,  # double-write semconv
            },
        )

    def _on_tool_result(self, event: ToolResolved) -> None:
        span = self._tools.pop(event.req_id, None)
        if span is None:
            return
        status = "ok" if event.ok else "error"
        span.set_attribute("gg_relay.tool_status", status)
        if event.error:
            span.set_attribute("gg_relay.tool_error", event.error[:512])
        span.end()

    # ── error events ──────────────────────────────────────────────────

    def _on_install_error(self, event: InstallError) -> None:
        parent = self._runs.get(event.session_id) or self._roots.get(
            event.session_id
        )
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
        """Forward-compat: untyped ``frame`` topic still surfaces errors."""
        sid = event.get("session_id")
        ftype = event.get("type")
        if not isinstance(sid, str) or not isinstance(ftype, str):
            return
        if ftype == "error":
            parent = self._runs.get(sid) or self._roots.get(sid)
            if parent is not None:
                parent.add_event(
                    "error",
                    attributes={
                        "code": str(event.get("code", "")),
                        "message": str(event.get("message", ""))[:512],
                    },
                )
