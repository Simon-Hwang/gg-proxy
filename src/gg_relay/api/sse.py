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
    """Render a typed ``RelayEvent`` (or legacy frame dict) as an SSE chunk."""
    if isinstance(event, ReplayedEvent):
        # Plan 7 D7.17: reconstructed events from the durable store.
        # The SSE id is "<seq>:<event_id>" so the next reconnect's
        # ``Last-Event-ID`` header drives ``bus.replay_after`` directly;
        # the original wire-level class name lives in ``type_name``.
        return ServerSentEvent(
            id=f"{event.seq}:{event.event_id}",
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


def _parse_durable_last_event_id(request: Request) -> int | None:
    """Parse the durable ``"<seq>:<event_id>"`` cursor (Plan 7 D7.17).

    Distinct from :func:`_parse_last_event_id` — that one parses the
    legacy per-session frame-seq cursor (``42`` / ``seq:42``) used by
    the in-session back-fill. The durable cursor uses the EventBus
    seq prefix + UUID suffix emitted by :func:`_event_to_sse` for
    :class:`ReplayedEvent` instances; only the seq half is needed for
    ``bus.replay_after``.

    Returns ``None`` when:

    * the header is missing,
    * it starts with ``"seq:"`` (legacy frame cursor — handled
      separately),
    * there's no ``:`` separator (so it's a bare int = frame cursor),
    * the prefix doesn't parse as an integer (garbage value — silently
      ignored so EventSource auto-reconnect with an old / opaque id
      degrades to a live tail rather than crashing).
    """
    header = request.headers.get("Last-Event-ID")
    if not header:
        return None
    value = header.strip()
    if value.startswith("seq:"):
        return None
    if ":" not in value:
        return None
    prefix = value.split(":", 1)[0]
    try:
        return int(prefix)
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
        # ── Plan 7 D7.17 (Task 13): durable-tier replay ──────────────
        # When the client supplies ``Last-Event-ID: <seq>:<uuid>``
        # (emitted by ReplayedEvent rendering) we walk the durable
        # store first to flush every persisted event with seq > cursor
        # in order. Filtered to ``session_id`` so multi-session feeds
        # stay isolated. Falls through silently when no durable store
        # is wired or the header is invalid — see
        # :func:`_parse_durable_last_event_id`.
        durable_seq = _parse_durable_last_event_id(request)
        if durable_seq is not None:
            async for evt in bus.replay_after(last_seq=durable_seq):
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
