"""Audit recording helpers — Plan 8 D8.4 (Task 5).

:class:`AuditService.record` is the canonical entry point for all
sensitive-mutation audit writes. The thin wrapper exists so business
code never reaches into :class:`SqlAlchemyStore` for audit (a future
swap to a different backend or a remote audit sink — Splunk / Loki /
Kafka — only has to replace this object on ``app.state``).

Recommended call shape (durable outbox; v2.1 MAJOR 3):

  * Open a transaction on the store layer for the mutation.
  * Call ``audit.record(..., conn=conn)`` within that SAME transaction.
  * Commit; if the commit fails, both the mutation and the audit row
    roll back together.

When ``conn`` is omitted the service falls back to opening its own
short-lived transaction. That is the path used by
:class:`gg_relay.api.middleware.audit.AuditFallbackMiddleware` (no
access to the manager's transaction); business code SHOULD pass
``conn`` whenever it can.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

logger = logging.getLogger("gg_relay.api.audit")


class _AuditStoreLike(Protocol):
    """Minimal structural type expected by :class:`AuditService`.

    Mirrors :meth:`gg_relay.store.protocol.AuditStore.record_audit` —
    duplicated here only so the service can stay free of an import-time
    cycle on the store package (the dashboard router and the
    middleware both import this module).
    """

    async def record_audit(
        self,
        *,
        actor: str,
        action: str,
        target_type: str | None = ...,
        target_id: str | None = ...,
        metadata: Mapping[str, Any] | None = ...,
        request_id: str | None = ...,
        ts: datetime | None = ...,
        conn: Any = ...,
    ) -> int: ...


class AuditService:
    """Thin facade over :meth:`AuditStore.record_audit`.

    Construction is cheap; one instance per process is the expected
    pattern (lifespan attaches it to ``app.state.audit_service``).
    """

    def __init__(self, store: _AuditStoreLike) -> None:
        self._store = store

    async def record(
        self,
        *,
        actor: str,
        action: str,
        target_type: str | None = None,
        target_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        conn: Any = None,
    ) -> int:
        """Record one audit event; return the new row's ``id``.

        ``actor`` is required. The API layer collapses unknown
        identities to ``"anon"`` so the column is always populated
        (the schema CHECK is non-NULL).

        Pass ``conn`` to write within the caller's existing
        transaction (durable outbox; v2.1 MAJOR 3). Without ``conn``
        the underlying store opens its own short-lived transaction.
        """
        return await self._store.record_audit(
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata,
            request_id=request_id,
            conn=conn,
        )


__all__ = ["AuditService"]
