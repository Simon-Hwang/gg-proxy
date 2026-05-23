"""AsyncEngine factory + helper to create all tables.

The ``create_all`` helper is used by tests (and by ``gg-relay migrate`` when
running against a fresh database without alembic history). Alembic is the
canonical migration path; ``create_all`` exists as a convenience for the
SQLite fixtures used by the unit tests.

Plan 8 D8.10 — :func:`make_async_engine` grew Postgres pool-tuning kwargs
(``pool_size`` / ``max_overflow`` / ``pool_pre_ping`` / ``pool_recycle``)
and a dialect-agnostic slow-query event listener (``slow_query_log_ms``).
The pool kwargs are only forwarded to ``create_async_engine`` for
``postgresql*`` URLs because SQLite's single-connection pool semantics
make them meaningless (and the QueuePool defaults are already correct
for dev / test). The slow-query listener attaches to the underlying
sync engine via SQLAlchemy's ``before_cursor_execute`` /
``after_cursor_execute`` hooks; setting ``slow_query_log_ms <= 0``
skips attaching entirely so there is zero per-query overhead.
"""
from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from gg_relay.store.schema import metadata

logger = logging.getLogger(__name__)


def make_async_engine(
    database_url: str,
    *,
    echo: bool = False,
    # ─── Plan 8 D8.10 Postgres pool tuning ───────────────────────────
    pool_size: int = 10,
    max_overflow: int = 5,
    pool_pre_ping: bool = True,
    pool_recycle: int = 3600,
    slow_query_log_ms: int = 500,
    **extra: Any,
) -> AsyncEngine:
    """Build an :class:`AsyncEngine` for the configured database URL.

    Accepts URLs like ``sqlite+aiosqlite:///./relay.db`` (dev) or
    ``postgresql+asyncpg://user:pass@host/db`` (prod).

    Tests that need an isolated SQLite database should use a temp file
    (``sqlite+aiosqlite:///{tmp_path}/relay.db``). Pure ``:memory:`` URLs
    work for sequential code but interleave badly under concurrent
    transactions because each pooled connection sees its own database;
    ``StaticPool`` would fix that but introduces transaction rollback races
    when the bg task and foreground poller share the single connection.

    Plan 8 D8.10 args:

    * ``pool_size`` / ``max_overflow`` / ``pool_pre_ping`` /
      ``pool_recycle`` — forwarded to :func:`create_async_engine` only
      for ``postgresql*`` URLs. SQLite's pool is effectively a single
      connection in async contexts, so these are silently dropped for
      SQLite URLs to avoid surprising operators who set them globally.
    * ``slow_query_log_ms`` — log any query whose elapsed wall-time
      exceeds this threshold (in milliseconds) at ``WARNING`` on the
      ``gg_relay.store.engine`` channel. ``<= 0`` disables the
      listener entirely.

    ``**extra`` is passed straight through to :func:`create_async_engine`
    so callers (or tests) can override anything we don't expose
    explicitly (``poolclass``, ``connect_args``, …).
    """
    engine_kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if database_url.startswith("postgresql"):
        engine_kwargs.update(
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=pool_pre_ping,
            pool_recycle=pool_recycle,
        )
    engine_kwargs.update(extra)

    engine = create_async_engine(database_url, **engine_kwargs)

    if slow_query_log_ms > 0:
        _attach_slow_query_listener(
            engine.sync_engine, threshold_ms=slow_query_log_ms
        )

    return engine


def _attach_slow_query_listener(engine: Engine, *, threshold_ms: int) -> None:
    """Attach ``before/after_cursor_execute`` hooks to log slow queries.

    The listener stashes a start timestamp on the ExecutionContext on
    ``before_cursor_execute`` and compares against ``perf_counter()`` in
    ``after_cursor_execute``. When elapsed >= ``threshold_ms`` we emit
    a ``slow_query`` WARN with a 200-char statement preview (long
    parameterised statements are common; capping prevents log
    explosion). The ``time.perf_counter`` reference is imported at
    module level so tests can stub it deterministically via
    ``monkeypatch.setattr('gg_relay.store.engine.perf_counter', …)``.
    """

    @event.listens_for(engine, "before_cursor_execute")
    def _on_before(  # pyright: ignore[reportUnusedFunction]
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        context._gg_query_start = perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _on_after(  # pyright: ignore[reportUnusedFunction]
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        start = getattr(context, "_gg_query_start", None)
        if start is None:
            return
        elapsed_ms = (perf_counter() - start) * 1000.0
        if elapsed_ms >= threshold_ms:
            stmt_preview = statement[:200].replace("\n", " ")
            logger.warning(
                "slow_query",
                extra={
                    "elapsed_ms": round(elapsed_ms, 1),
                    "threshold_ms": threshold_ms,
                    "statement_preview": stmt_preview,
                },
            )


async def create_all_tables(engine: AsyncEngine) -> None:
    """Create every table in :data:`gg_relay.store.schema.metadata`.

    Idempotent. Used by tests and the SQLite dev path; production
    deployments should rely on Alembic migrations instead.
    """
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
