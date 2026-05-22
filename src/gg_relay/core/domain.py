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

    Plan 6 D6.1 adds the explicit ``PAUSED`` state for user-initiated
    interrupt/resume. The allowed transition table is exposed below as
    :data:`LEGAL_TRANSITIONS` and enforced by :func:`is_legal_transition`
    so SessionManager (and any future state machine helpers / migrations)
    can validate edges without re-deriving the table.

      ``QUEUED → {RUNNING, CANCELLED}``
      ``RUNNING → {PAUSED, COMPLETED, FAILED, CANCELLED, INTERRUPTED}``
      ``PAUSED → {RUNNING, CANCELLED}``  *(NEW Plan 6 D6.1)*
      Terminal states (COMPLETED / FAILED / CANCELLED / INTERRUPTED) accept
      no further outgoing transitions.
    """

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
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


LEGAL_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.QUEUED: frozenset(
        {SessionState.RUNNING, SessionState.CANCELLED, SessionState.INTERRUPTED}
    ),
    SessionState.RUNNING: frozenset(
        {
            SessionState.PAUSED,
            SessionState.COMPLETED,
            SessionState.FAILED,
            SessionState.CANCELLED,
            SessionState.INTERRUPTED,
        }
    ),
    SessionState.PAUSED: frozenset(
        {SessionState.RUNNING, SessionState.CANCELLED, SessionState.INTERRUPTED}
    ),
    SessionState.COMPLETED: frozenset(),
    SessionState.FAILED: frozenset(),
    SessionState.CANCELLED: frozenset(),
    SessionState.INTERRUPTED: frozenset(),
}
"""Authoritative lifecycle edge table (D6.1).

The ``INTERRUPTED`` outgoing edge from QUEUED/RUNNING/PAUSED preserves
the Plan 4 crash-recovery semantics: ``Repository.mark_in_flight_as_interrupted``
moves any in-flight row to INTERRUPTED at startup, independent of which
non-terminal state it was in.

Read-only at runtime — mutate the source dict and the static analysis
above (StrEnum members + LEGAL_TRANSITIONS shape) goes out of sync.
"""


def is_legal_transition(from_state: SessionState, to_state: SessionState) -> bool:
    """Return whether moving from ``from_state`` to ``to_state`` is allowed.

    Used by SessionManager.pause/resume guards so we never silently land
    a session in a logically impossible state (e.g. PAUSED → COMPLETED
    would skip the RUNNING → COMPLETED runner-driven path).
    """
    return to_state in LEGAL_TRANSITIONS.get(from_state, frozenset())


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
