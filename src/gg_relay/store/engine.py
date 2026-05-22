"""AsyncEngine factory + helper to create all tables.

The ``create_all`` helper is used by tests (and by ``gg-relay migrate`` when
running against a fresh database without alembic history). Alembic is the
canonical migration path; ``create_all`` exists as a convenience for the
SQLite fixtures used by the unit tests.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from gg_relay.store.schema import metadata


def make_async_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Build an :class:`AsyncEngine` for the configured database URL.

    Accepts URLs like ``sqlite+aiosqlite:///./relay.db`` (dev) or
    ``postgresql+asyncpg://user:pass@host/db`` (prod).

    Tests that need an isolated SQLite database should use a temp file
    (``sqlite+aiosqlite:///{tmp_path}/relay.db``). Pure ``:memory:`` URLs
    work for sequential code but interleave badly under concurrent
    transactions because each pooled connection sees its own database;
    ``StaticPool`` would fix that but introduces transaction rollback races
    when the bg task and foreground poller share the single connection.
    """
    return create_async_engine(database_url, echo=echo, future=True)


async def create_all_tables(engine: AsyncEngine) -> None:
    """Create every table in :data:`gg_relay.store.schema.metadata`.

    Idempotent. Used by tests and the SQLite dev path; production
    deployments should rely on Alembic migrations instead.
    """
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
