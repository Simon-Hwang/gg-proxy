"""``GET /metrics`` — Prometheus scrape endpoint (Plan 5 Task 6).

Returns the process-wide ``REGISTRY`` defined in
``gg_relay.tracing.metrics``. No authentication is required here so the
endpoint can be scraped by a Prometheus sidecar; production deployments
should restrict the route via reverse proxy / network ACLs.
"""
from __future__ import annotations

from fastapi import APIRouter, Response

from gg_relay.tracing.metrics import render

metrics_router = APIRouter(tags=["metrics"])


@metrics_router.get("/metrics", include_in_schema=False)
def metrics_endpoint() -> Response:
    body, content_type = render()
    return Response(content=body, media_type=content_type)
