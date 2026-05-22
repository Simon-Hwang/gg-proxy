"""Core domain enums + summary dataclasses shared across modules.

Imported by ``store`` (status persistence), ``session.manager`` (state
transitions), ``api.schemas`` (Pydantic IO), and ``dashboard`` (template
rendering). Kept dependency-free so it can be imported anywhere without
pulling in SQLAlchemy / FastAPI.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SessionState(StrEnum):
    """Lifecycle of a SessionManager-tracked session.

    Transitions allowed (enforced by SessionManager._run, not the enum):

      ``QUEUED → RUNNING → {COMPLETED, FAILED, CANCELLED}``
      Any state at startup-recovery boundary → ``INTERRUPTED``
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_STATES: frozenset[SessionState] = frozenset(
    {
        SessionState.COMPLETED,
        SessionState.FAILED,
        SessionState.CANCELLED,
        SessionState.INTERRUPTED,
    }
)


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Row-shaped projection returned by SessionManager.list().

    Subset of the ``sessions`` table — intentionally excludes ``spec_json``
    so list endpoints don't accidentally ship raw (redacted) spec data
    over the wire on hot paths. Dashboards that want the spec must call
    ``get_session(id)`` instead.
    """

    id: str
    status: SessionState
    submitted_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    tags: tuple[str, ...] = ()
    backend: str = ""
    end_reason: str | None = None
