"""FastAPI routers (public surface)."""
from gg_relay.api.routers.audit import router as audit_router
from gg_relay.api.routers.comments import router as comments_router
from gg_relay.api.routers.events import router as events_router
from gg_relay.api.routers.health import router as health_router
from gg_relay.api.routers.hitl import router as hitl_router
from gg_relay.api.routers.metrics import metrics_router
from gg_relay.api.routers.sessions import router as sessions_router

__all__ = [
    "audit_router",
    "comments_router",
    "events_router",
    "health_router",
    "hitl_router",
    "metrics_router",
    "sessions_router",
]
