"""FastAPI routers (public surface)."""
from gg_relay.api.routers.admin_drain import router as admin_drain_router
from gg_relay.api.routers.admin_keys import router as admin_keys_router
from gg_relay.api.routers.audit import router as audit_router
from gg_relay.api.routers.comments import router as comments_router
from gg_relay.api.routers.cost import router as cost_router
from gg_relay.api.routers.events import router as events_router
from gg_relay.api.routers.health import router as health_router
from gg_relay.api.routers.hitl import batch_router as hitl_batch_router
from gg_relay.api.routers.hitl import router as hitl_router
from gg_relay.api.routers.metrics import metrics_router
from gg_relay.api.routers.sessions import router as sessions_router
from gg_relay.api.routers.templates import router as templates_router

__all__ = [
    "admin_drain_router",
    "admin_keys_router",
    "audit_router",
    "comments_router",
    "cost_router",
    "events_router",
    "health_router",
    "hitl_batch_router",
    "hitl_router",
    "metrics_router",
    "sessions_router",
    "templates_router",
]
