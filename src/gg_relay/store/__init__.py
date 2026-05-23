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
  - :class:`CursorInvalidError` /
    :class:`CursorFilterMismatchError`
                                    — Plan 7 D7.6 cursor pagination
                                      errors raised by
                                      ``list_sessions(after=...)``
  - :class:`ConcurrencyError`       — Plan 7 D7.5 optimistic-locking
                                      mismatch raised by
                                      ``update_session_status(expected_version=...)``
                                      and ``upsert_hitl(expected_version=...)``
"""
from gg_relay.store.engine import create_all_tables, make_async_engine
from gg_relay.store.exceptions import (
    ConcurrencyError,
    CursorFilterMismatchError,
    CursorInvalidError,
)
from gg_relay.store.protocol import (
    AuditStore,
    CommentStore,
    FrameStore,
    HITLStore,
    SessionStore,
)
from gg_relay.store.repository import SessionRepository, SqlAlchemyStore
from gg_relay.store.schema import (
    audit_log,
    frames,
    hitl_requests,
    metadata,
    session_comments,
    sessions,
)

__all__ = [
    "AuditStore",
    "CommentStore",
    "ConcurrencyError",
    "CursorFilterMismatchError",
    "CursorInvalidError",
    "FrameStore",
    "HITLStore",
    "SessionRepository",
    "SessionStore",
    "SqlAlchemyStore",
    "audit_log",
    "create_all_tables",
    "frames",
    "hitl_requests",
    "make_async_engine",
    "metadata",
    "session_comments",
    "sessions",
]
