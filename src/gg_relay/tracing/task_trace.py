"""gg-task-trace.v1 JSONL subscriber (Plan 5 D5.7=A + D5.16).

Drains the EventBus and writes one JSON-Lines record per lifecycle event
to ``Config.task_trace_path`` (default
``~/.claude/metrics/gg-task-trace.jsonl``) using the ``gg.task-trace.v1``
schema. Compatible with ``/gg:task-trace latest`` in gg-plugins.

Multi-instance safety:

* The default path **is shared across every gg-relay process running on
  the same host**, which is fine for dev but dangerous in production —
  concurrent writes can interleave lines. The recommended production
  configuration is documented in ``docs/deployment.md``; the salient
  options are:
    1. ``Config.task_trace_path = None`` → writer is *disabled*.
    2. Set a host-unique path per instance (``${hostname}-trace.jsonl``).
* Each process serialises its own writes with an ``asyncio.Lock``; the
  actual file append happens on a thread (``asyncio.to_thread``) so the
  event-loop is never blocked on disk I/O.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from gg_relay.core import (
    EventBusBackend,
    HITLRequested,
    HITLResolved,
    InstallError,
    RelayEvent,
    SessionCompleted,
    SessionCreated,
    SessionStateChanged,
    ToolRequested,
    ToolResolved,
)

logger = logging.getLogger("gg_relay.tracing.task_trace")

SCHEMA_VERSION = "gg.task-trace.v1"
DEFAULT_PATH = Path.home() / ".claude" / "metrics" / "gg-task-trace.jsonl"


def _base_record(event: RelayEvent, event_type: str) -> dict[str, object]:
    """Build the shared envelope fields. ``traceId`` is the session id."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "eventType": event_type,
        "traceId": getattr(event, "session_id", "") or "",
        "timestamp": event.occurred_at.isoformat(),
        "source": "gg-relay",
    }


class TaskTraceSubscriber:
    """EventBus subscriber that writes JSONL lifecycle records.

    Construct once, then either:

      * ``asyncio.create_task(subscriber.consume(bus))`` — long-running
        background task; cancel on shutdown.
      * ``await subscriber.write_event(event)`` — synchronous one-shot
        for tests / replay scripts.

    ``path=None`` disables the writer (per Plan 5 D5.16); callers can
    still construct the subscriber so wiring stays simple, but
    :meth:`write_event` becomes a no-op.
    """

    def __init__(self, *, path: Path | None = DEFAULT_PATH) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:  # pragma: no cover - defensive
                logger.warning(
                    "task_trace: cannot create parent dir for %s: %s",
                    path,
                    exc,
                )

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def disabled(self) -> bool:
        return self._path is None

    async def consume(self, bus: EventBusBackend) -> None:
        """Drain ``bus`` until close. Ignores events without a render."""
        async for event in bus.subscribe("*"):
            if isinstance(event, RelayEvent):
                await self.write_event(event)

    def render(self, event: RelayEvent) -> Mapping[str, object] | None:
        """Map a typed event to its task-trace.v1 JSON dict (or None to skip)."""
        if isinstance(event, SessionCreated):
            rec = _base_record(event, "session.created")
            rec["tags"] = list(event.tags)
            rec["prompt_redacted"] = event.prompt_redacted
            return rec
        if isinstance(event, SessionStateChanged):
            rec = _base_record(event, f"session.state.{event.to_state}")
            rec["from_state"] = event.from_state
            rec["to_state"] = event.to_state
            if event.reason is not None:
                rec["reason"] = event.reason
            return rec
        if isinstance(event, SessionCompleted):
            rec = _base_record(event, "session.completed")
            rec["status"] = event.status
            rec["tokens"] = dict(event.tokens)
            rec["cost_usd"] = event.cost_usd
            return rec
        if isinstance(event, HITLRequested):
            rec = _base_record(event, "hitl.requested")
            rec["req_id"] = event.req_id
            rec["tool"] = event.tool
            return rec
        if isinstance(event, HITLResolved):
            rec = _base_record(event, "hitl.resolved")
            rec["req_id"] = event.req_id
            rec["decision"] = event.decision
            if event.reason is not None:
                rec["reason"] = event.reason
            return rec
        if isinstance(event, ToolRequested):
            rec = _base_record(event, "tool.requested")
            rec["req_id"] = event.req_id
            rec["tool"] = event.tool
            return rec
        if isinstance(event, ToolResolved):
            rec = _base_record(event, "tool.resolved")
            rec["req_id"] = event.req_id
            rec["ok"] = event.ok
            return rec
        if isinstance(event, InstallError):
            rec = _base_record(event, "error")
            rec["code"] = event.code
            rec["message"] = event.message
            return rec
        # SessionOutputChunk / Heartbeat / InstallDone — too chatty to
        # persist line-by-line; the lifecycle records above are enough
        # for ``/gg:task-trace latest``.
        return None

    async def write_event(self, event: RelayEvent) -> None:
        """Render + serialise + append. No-op when ``disabled``."""
        if self._path is None:
            return
        record = self.render(event)
        if record is None:
            return
        line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        async with self._lock:
            await asyncio.to_thread(self._append_line, line)

    def _append_line(self, line: str) -> None:
        assert self._path is not None  # noqa: S101 — for type narrowing only
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line)

    def _serialize_for_debug(self, event: RelayEvent) -> str:  # pragma: no cover
        return json.dumps(asdict(event), default=str)
