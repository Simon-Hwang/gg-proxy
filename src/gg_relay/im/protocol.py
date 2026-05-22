"""Plugin-style protocol for IM backends.

The protocol is intentionally narrow — we only need two notifications:
HITL pending (with an actionable card) and session end (informational).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class IMBackend(Protocol):
    """Outbound messenger surface (Feishu, future Slack, etc.)."""

    name: str

    async def notify_hitl_pending(
        self,
        *,
        session_id: str,
        req_id: str,
        tool: str,
        args_summary: str,
        callback_base: str,
    ) -> None: ...

    async def notify_session_end(
        self,
        *,
        session_id: str,
        status: str,
        summary: str,
    ) -> None: ...
