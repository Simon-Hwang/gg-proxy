"""gg_relay.store — async persistence layer.

Public surface used by SessionManager + API routers:

  - :data:`metadata`                — SQLAlchemy MetaData for migrations
  - :func:`make_async_engine`       — AsyncEngine factory
  - :func:`create_all_tables`       — used by tests / SQLite dev path
  - :class:`SqlAlchemyStore`        — concrete async DAO over the three tables
  - :class:`SessionStore` / :class:`FrameStore` / :class:`HITLStore`
                                    — Plan 7 D7.4 Protocols (split surface)
  - :class:`SessionRepository`      — deprecated alias for
                                      :class:`SqlAlchemyStore` (warns on
                                      instantiation; removed in 0.8.0)
"""
from gg_relay.store.engine import create_all_tables, make_async_engine
from gg_relay.store.protocol import FrameStore, HITLStore, SessionStore
from gg_relay.store.repository import SessionRepository, SqlAlchemyStore
from gg_relay.store.schema import frames, hitl_requests, metadata, sessions

__all__ = [
    "FrameStore",
    "HITLStore",
    "SessionRepository",
    "SessionStore",
    "SqlAlchemyStore",
    "create_all_tables",
    "frames",
    "hitl_requests",
    "make_async_engine",
    "metadata",
    "sessions",
]
