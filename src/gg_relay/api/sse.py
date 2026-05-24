"""Server-Sent Events helpers (Plan 5 D5.4=A + filter + Last-Event-ID).

The single entry point is :func:`session_event_stream` which builds an
``EventSourceResponse`` filtered to a given ``session_id``. Three
behaviours worth flagging:

1. **SSE event field** = ``type(event).__name__`` (e.g. ``SessionCreated``)
   so consumers can switch on event class without parsing data.
2. **Event id** = ``event.event_id`` UUID stringified. Per-event IDs let
   future Plan 7+ event-log indexing reconnect missed events by event_id;
   the v1 cursor uses *frame seq* (see below).
3. **Last-Event-ID back-fill** is best-effort: when the header is parseable
   as an integer the endpoint emits stored frames with ``seq > N`` from
   the persistence layer *before* attaching the live bus subscriber. Frame
   seqs are per-session monotonic (Plan 4 store schema enforces this), so
   ``seq`` is a stable cursor for the lifetime of a single session.

When the client disconnects the underlying async generator's ``finally``
clause invokes the EventBus subscriber's ``aclose``, which removes its
deque from ``EventBus._subs`` so we don't leak buffers.
"""
from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

from sse_starlette.sse import (  # type: ignore[attr-defined]
    EventSourceResponse,
    ServerSentEvent,
)
from starlette.requests import Request

from gg_relay.core import EventBus, RelayEvent
from gg_relay.store import SessionRepository
from gg_relay.store.durable_event import ReplayedEvent


def _event_to_sse(event: RelayEvent | Mapping[str, Any]) -> ServerSentEvent | None:
    """Render a typed ``RelayEvent`` (or legacy frame dict) as an SSE chunk.

    Plan 9 v0.9.0-rc D9.9a — the SSE id for :class:`ReplayedEvent` is
    now emitted in the v2 format ``"v2:<row-seq>:<event_id>"`` so the
    next reconnect drives :meth:`EventBus.replay_after_seq`. v0.8.x
    cursors (``"<microsecond-seq>:<event_id>"`` without the ``v2:``
    prefix) continue to be parseable by
    :func:`_parse_durable_last_event_id` for the backward-compat
    window (≥2 minor releases).
    """
    if isinstance(event, ReplayedEvent):
        return ServerSentEvent(
            id=f"v2:{event.seq}:{event.event_id}",
            event=event.type_name or "RelayEvent",
            data=json.dumps(event.payload, default=str),
        )
    if isinstance(event, RelayEvent):
        return ServerSentEvent(
            id=str(event.event_id),
            event=type(event).__name__,
            data=json.dumps(dataclasses.asdict(event), default=str),
        )
    if isinstance(event, Mapping):
        ftype = str(event.get("type", "frame"))
        seq = event.get("seq")
        sse_id = f"seq:{seq}" if isinstance(seq, int) else None
        return ServerSentEvent(
            id=sse_id,
            event=f"frame.{ftype}",
            data=json.dumps(dict(event), default=str),
        )
    return None


def _parse_last_event_id(request: Request) -> int | None:
    """Parse a numeric frame-seq cursor out of ``Last-Event-ID``.

    Accepts either the bare integer form (``42``) or the ``"seq:42"``
    prefixed form emitted by legacy str-topic frames. Returns ``None``
    when the header is missing or unparseable so the caller can skip the
    back-fill step entirely.
    """
    header = request.headers.get("Last-Event-ID")
    if not header:
        return None
    value = header.strip()
    if value.startswith("seq:"):
        value = value[4:]
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_durable_last_event_id(
    request: Request,
) -> tuple[int, int] | None:
    """Parse the durable cursor with schema version (Plan 7 + Plan 9 D9.9a).

    Returns ``(schema_version, last_seq)`` so the caller can dispatch
    between two replay paths:

    * ``schema_version == 1`` — Plan 7 D7.17 v1 cursor
      ``"<microsecond-seq>:<event_id>"``. The seq is a microsecond
      timestamp; replay uses :meth:`EventBus.replay_after`.
    * ``schema_version == 2`` — Plan 9 D9.9a v2 cursor
      ``"v2:<row-seq>:<event_id>"``. The seq is the strictly
      monotonic ``events.seq`` column populated by Alembic 0012a;
      replay uses :meth:`EventBus.replay_after_seq`.

    Compatibility window: v0.9.0+ servers emit v2 cursors going
    forward; v0.8.x clients reconnecting after upgrade still send v1
    cursors and walk the microsecond path. v0.10.0 may freeze the v1
    path; v0.11.0 may remove it (≥2-minor compat window).

    Returns ``None`` when:

    * the header is missing,
    * the header is the legacy per-session ``"seq:<n>"`` frame cursor
      (handled by :func:`_parse_last_event_id`),
    * the format is unrecognisable (garbage value → degrade to live
      tail rather than crash).
    """
    header = request.headers.get("Last-Event-ID")
    if not header:
        return None
    value = header.strip()
    if value.startswith("seq:"):
        # Legacy per-session frame cursor — parsed elsewhere.
        return None
    if value.startswith("v2:"):
        # v2 cursor: "v2:<row-seq>:<event_id>" or "v2:<row-seq>".
        tail = value[3:]
        prefix = tail.split(":", 1)[0]
        try:
            return (2, int(prefix))
        except ValueError:
            return None
    if ":" not in value:
        # Bare integer — could be a v1 cursor without event_id
        # suffix (rare) or a legacy frame cursor without ``seq:``
        # prefix. Treat as v1 to keep the existing behavior.
        return None
    prefix = value.split(":", 1)[0]
    try:
        return (1, int(prefix))
    except ValueError:
        return None


async def _stream(
    bus: EventBus,
    store: SessionRepository,
    session_id: str,
    request: Request,
    *,
    initial_buffer_size: int = 1024,
) -> AsyncIterator[ServerSentEvent]:
    """Generator: backfill missed frames, then bridge live EventBus events."""
    # Subscribe FIRST so events arriving during back-fill aren't lost.
    sub = bus.subscribe("*", maxsize=initial_buffer_size)
    try:
        # ── Plan 7 D7.17 + Plan 9 D9.9a: durable-tier replay ─────────
        # When the client supplies ``Last-Event-ID: <seq>:<uuid>``
        # (v1 microsecond cursor) OR ``Last-Event-ID: v2:<seq>:<uuid>``
        # (v2 row-seq cursor) we walk the durable store first to
        # flush every persisted event past the cursor in order.
        # Filtered to ``session_id`` so multi-session feeds stay
        # isolated. Falls through silently when no durable store is
        # wired or the header is invalid — see
        # :func:`_parse_durable_last_event_id`.
        cursor = _parse_durable_last_event_id(request)
        if cursor is not None:
            schema_version, durable_seq = cursor
            if schema_version == 2:
                # Plan 9 D9.9a — strict row-seq cursor (post-0012a).
                replay_iter = bus.replay_after_seq(last_seq=durable_seq)
            else:
                # v1 microsecond cursor — backward-compat path.
                replay_iter = bus.replay_after(last_seq=durable_seq)
            async for evt in replay_iter:
                if isinstance(evt, ReplayedEvent):
                    if evt.session_id and evt.session_id != session_id:
                        continue
                elif isinstance(evt, RelayEvent):
                    if getattr(evt, "session_id", None) != session_id:
                        continue
                else:
                    continue
                sse = _event_to_sse(evt)
                if sse is not None:
                    yield sse

        last_seq = _parse_last_event_id(request)
        if last_seq is not None:
            rows = await store.list_frames(
                session_id, limit=1000, offset=0
            )
            for row in rows:
                row_seq = int(row.get("seq", 0))
                if row_seq <= last_seq:
                    continue
                payload = dict(row.get("payload") or {})
                payload.setdefault("type", row.get("type"))
                payload.setdefault("seq", row_seq)
                payload["session_id"] = session_id
                sse = _event_to_sse(payload)
                if sse is not None:
                    yield sse

        async for event in sub:
            if isinstance(event, RelayEvent):
                if getattr(event, "session_id", None) != session_id:
                    continue
            elif isinstance(event, Mapping):
                if event.get("session_id") != session_id:
                    continue
            else:
                continue
            sse = _event_to_sse(event)
            if sse is not None:
                yield sse
    finally:
        # Drop the subscription so its deque stops accumulating events.
        await sub.aclose()  # type: ignore[attr-defined]


def session_event_stream(
    bus: EventBus,
    store: SessionRepository,
    session_id: str,
    request: Request,
    *,
    heartbeat_s: float = 15.0,
) -> EventSourceResponse:
    """Build an SSE response that streams ``session_id`` events.

    ``heartbeat_s`` is the comment-frame keep-alive sent by sse-starlette
    so intermediate proxies (nginx, Cloudflare) don't close idle
    connections. A value of 15s keeps cleanly under typical 30-60s
    timeouts while not adding noticeable bandwidth.
    """
    return EventSourceResponse(
        _stream(bus, store, session_id, request),
        ping=int(heartbeat_s),
    )
