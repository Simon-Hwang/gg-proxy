"""gg_relay.store — async persistence layer.

Public surface used by SessionManager + API routers:

  - :data:`metadata`                — SQLAlchemy MetaData for migrations
  - :func:`make_async_engine`       — AsyncEngine factory
  - :func:`create_all_tables`       — used by tests / SQLite dev path
  - :class:`SessionRepository`      — async DAO for the three tables
"""
from gg_relay.store.engine import create_all_tables, make_async_engine
from gg_relay.store.repository import SessionRepository
from gg_relay.store.schema import frames, hitl_requests, metadata, sessions

__all__ = [
    "SessionRepository",
    "create_all_tables",
    "frames",
    "hitl_requests",
    "make_async_engine",
    "metadata",
    "sessions",
]
