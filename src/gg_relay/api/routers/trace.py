"""Trace analytics endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from gg_relay.api.dependencies.require_role import require_role

router = APIRouter(prefix="/trace", tags=["trace"])


@router.get(
    "/patterns",
    dependencies=[Depends(require_role("admin"))],
)
async def trace_patterns(
    request: Request,
    since: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Return high-frequency tool/input hash patterns from SDK hooks."""
    rows = await request.app.state.store.aggregate_tool_patterns(
        since=since,
        limit=limit,
    )
    return {
        "items": [
            {
                "tool_name": row["tool_name"],
                "input_hash": row["input_hash"],
                "count": row["count"],
            }
            for row in rows
        ]
    }
