"""SSE event stream endpoint (Plan 5 D5.4=A).

``GET /api/v1/sessions/{id}/events`` — Server-Sent Events stream filtered
to a single session. Supports ``Last-Event-ID`` reconnection via the
shared :mod:`gg_relay.api.sse` helper.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from gg_relay.api.sse import session_event_stream

router = APIRouter(prefix="/sessions", tags=["events"])


@router.get("/{session_id}/events", response_class=EventSourceResponse)
async def stream_session_events(
    session_id: str, request: Request
) -> EventSourceResponse:
    bus = request.app.state.bus
    store = request.app.state.store
    row = await store.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session_event_stream(bus, store, session_id, request)
