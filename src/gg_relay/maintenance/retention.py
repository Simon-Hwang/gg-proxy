"""Retention cleanup for observability tables — Plan 8 Task 20 / D8.3.

Drops old rows from the durable event store, audit log, and resolved
HITL request log so a long-running deployment doesn't accumulate
unbounded telemetry. Designed to run from a periodic scheduler
(systemd timer, cron, k8s ``CronJob``); the CLI wrapper
``gg-relay maintenance`` exposes ``--dry-run`` for preview.

Defaults match v2.4 §7 Phase 5 Task 20:

* ``events``        — 30 days   (durable event store)
* ``audit_log``     — 90 days   (compliance trail, kept longer)
* ``hitl_requests`` — 30 days after ``resolved_at`` (unresolved kept)

Cross-dialect batched DELETE pattern:

    DELETE FROM <t> WHERE <pk> IN (
        SELECT <pk> FROM <t>
        WHERE <where_col> < :cutoff
        [AND <extra_filter>]
        LIMIT :batch_size
    )

This shape works on both SQLite and Postgres without dialect-specific
``DELETE ... LIMIT`` syntax. The batch loop stops as soon as a batch
returns fewer than ``batch_size`` rows so the last iteration on a
small table is also short.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import Column, Table, delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("gg_relay.maintenance.retention")


@dataclass(frozen=True)
class RetentionSummary:
    """Per-table retention summary surfaced to the CLI / log line.

    Attributes:
        table: SQL table name (``"events"`` / ``"audit_log"`` / ``"hitl_requests"``).
        cutoff: UTC instant used in the ``< cutoff`` predicate.
        rows_deleted: Number of rows removed (or projected, when
            ``dry_run=True``).
        batches: How many ``DELETE`` batches were executed (or the
            number of preview batches for ``dry_run``).
        dry_run: ``True`` when no DELETE was actually issued.
    """

    table: str
    cutoff: datetime
    rows_deleted: int
    batches: int
    dry_run: bool


@dataclass(frozen=True)
class RetentionResult:
    """Aggregate result for a single ``run_retention`` invocation."""

    summaries: tuple[RetentionSummary, ...]
    total_deleted: int
    dry_run: bool


async def run_retention(
    *,
    engine: AsyncEngine,
    events_days: int = 30,
    audit_log_days: int = 90,
    hitl_resolved_days: int = 30,
    batch_size: int = 10000,
    dry_run: bool = False,
) -> RetentionResult:
    """Prune ``events`` / ``audit_log`` / ``hitl_requests`` past their cutoffs.

    Args:
        engine: AsyncEngine to issue the batched DELETE / SELECT pairs against.
        events_days: Drop ``events`` rows whose ``ts`` is older than
            ``now - events_days``.
        audit_log_days: Drop ``audit_log`` rows whose ``ts`` is older than
            ``now - audit_log_days``.
        hitl_resolved_days: Drop ``hitl_requests`` rows whose
            ``resolved_at`` is non-NULL **and** older than
            ``now - hitl_resolved_days``. Unresolved (``resolved_at IS NULL``)
            rows are preserved.
        batch_size: Per-batch ``LIMIT`` so a multi-million-row prune
            doesn't hold a long lock. Defaults to 10 000.
        dry_run: When ``True``, count rows that would be deleted (one
            preview batch per table) but do not issue the DELETE.

    Returns:
        ``RetentionResult`` carrying one ``RetentionSummary`` per table
        plus the grand total.
    """
    from gg_relay.store.schema import audit_log, events, hitl_requests

    now = datetime.now(timezone.utc)
    summaries: list[RetentionSummary] = []

    events_cutoff = now - timedelta(days=events_days)
    events_summary = await _delete_in_batches(
        engine,
        table=events,
        pk_col=events.c.event_id,
        where_col=events.c.ts,
        cutoff=events_cutoff,
        batch_size=batch_size,
        dry_run=dry_run,
        table_name="events",
    )
    summaries.append(events_summary)

    audit_cutoff = now - timedelta(days=audit_log_days)
    audit_summary = await _delete_in_batches(
        engine,
        table=audit_log,
        pk_col=audit_log.c.id,
        where_col=audit_log.c.ts,
        cutoff=audit_cutoff,
        batch_size=batch_size,
        dry_run=dry_run,
        table_name="audit_log",
    )
    summaries.append(audit_summary)

    hitl_cutoff = now - timedelta(days=hitl_resolved_days)
    hitl_summary = await _delete_in_batches(
        engine,
        table=hitl_requests,
        pk_col=hitl_requests.c.id,
        where_col=hitl_requests.c.resolved_at,
        cutoff=hitl_cutoff,
        batch_size=batch_size,
        dry_run=dry_run,
        table_name="hitl_requests",
        extra_filter=lambda t: t.c.resolved_at.isnot(None),
    )
    summaries.append(hitl_summary)

    total = sum(s.rows_deleted for s in summaries)
    logger.info(
        "retention run dry_run=%s total_deleted=%d (%s)",
        dry_run,
        total,
        ", ".join(f"{s.table}={s.rows_deleted}" for s in summaries),
    )
    return RetentionResult(
        summaries=tuple(summaries),
        total_deleted=total,
        dry_run=dry_run,
    )


async def _delete_in_batches(
    engine: AsyncEngine,
    *,
    table: Table,
    pk_col: Column[Any],
    where_col: Column[Any],
    cutoff: datetime,
    batch_size: int,
    dry_run: bool,
    table_name: str,
    extra_filter: Callable[[Table], Any] | None = None,
) -> RetentionSummary:
    """Delete rows where ``where_col < cutoff`` in ``LIMIT batch_size`` chunks.

    Uses the cross-dialect ``DELETE ... WHERE pk IN (SELECT pk ... LIMIT N)``
    pattern. ``pk_col`` must be provided because the ``events`` table
    uses ``event_id`` as its primary key while ``audit_log`` /
    ``hitl_requests`` use ``id``.

    On ``dry_run=True`` we issue a single bounded SELECT to estimate
    how many rows would go and return without deleting; this keeps
    the preview cheap on large tables and matches the operator
    expectation that ``--dry-run`` does not touch the database.
    """
    total_deleted = 0
    batches = 0

    while True:
        sub_stmt = select(pk_col).where(where_col < cutoff).limit(batch_size)
        if extra_filter is not None:
            sub_stmt = sub_stmt.where(extra_filter(table))

        async with engine.connect() as conn:
            result = await conn.execute(sub_stmt)
            ids_to_delete = [row[0] for row in result.all()]

        if not ids_to_delete:
            break

        if dry_run:
            total_deleted += len(ids_to_delete)
            batches += 1
            break

        async with engine.begin() as conn:
            del_stmt = delete(table).where(pk_col.in_(ids_to_delete))
            del_result = await conn.execute(del_stmt)
            # ``rowcount`` is reliable on the SQLite + Postgres async
            # drivers we ship; fall back to the IN-list length on the
            # off-chance a driver returns ``-1``.
            affected = del_result.rowcount
            if affected is None or affected < 0:
                affected = len(ids_to_delete)
            total_deleted += affected
            batches += 1

        if len(ids_to_delete) < batch_size:
            break

    return RetentionSummary(
        table=table_name,
        cutoff=cutoff,
        rows_deleted=total_deleted,
        batches=batches,
        dry_run=dry_run,
    )
