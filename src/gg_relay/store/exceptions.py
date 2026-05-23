"""Store-layer exceptions — Plan 7 Tasks 8 + 9 (D7.5 / D7.6).

* Cursor-pagination errors raised by :meth:`SqlAlchemyStore.list_sessions`
  when a client-supplied ``after=`` cursor is malformed or no longer
  matches the active filter combination (Task 9, D7.6).
* :class:`ConcurrencyError` raised by version-checked writes
  (:meth:`SqlAlchemyStore.update_session_status` and
  :meth:`SqlAlchemyStore.upsert_hitl`) when an optimistic-lock
  ``WHERE version = :expected`` clause matches zero rows — i.e. another
  writer bumped the row's ``version`` between read and write
  (Task 8, D7.5).

Both cursor classes inherit from :class:`ValueError` so unwary callers
that catch the broad base class still see them as input-validation
failures; the FastAPI router catches the specific subclasses and maps
them to ``400`` with a discriminating ``code`` field.

:class:`ConcurrencyError` is a plain :class:`Exception` — callers MUST
handle it explicitly (no implicit ``ValueError`` swallow) so a missed
``except`` block is loud at runtime instead of silently corrupting
state.
"""
from __future__ import annotations


class CursorInvalidError(ValueError):
    """The opaque cursor blob could not be decoded.

    Raised when the urlsafe-base64 payload is truncated, contains
    non-base64 characters, or its JSON body is missing the expected
    ``ts`` / ``id`` / ``fh`` fields. Maps to HTTP 400 with code
    ``cursor_invalid``.
    """


class CursorFilterMismatchError(ValueError):
    """The cursor was issued against a different filter combination.

    Cursors are bound to the filter hash (``status`` + ``tag``) they
    were generated under so paging across a status change is detected
    and rejected rather than silently returning a confusing mix. Maps
    to HTTP 400 with code ``cursor_filter_mismatch``.
    """


class ConcurrencyError(Exception):
    """A version-checked write found a stale row (Plan 7 D7.5 / Task 8).

    Optimistic-locking pattern: the caller reads ``version = V``,
    attempts ``UPDATE ... WHERE version = V``. If zero rows are
    affected, another writer bumped the version in between (or the
    row was deleted). The caller decides whether to retry with the
    freshly-read version (managed retries for session state
    transitions) or surface 409 to the user (HITL resolve).

    The ``expected_version`` / ``actual_version`` attributes let
    higher layers craft a meaningful error response (e.g. the HITL
    409 body carries the *first* decision so the loser sees what
    won the race).
    """

    def __init__(
        self,
        msg: str,
        *,
        expected_version: int | None = None,
        actual_version: int | None = None,
    ) -> None:
        super().__init__(msg)
        self.expected_version = expected_version
        self.actual_version = actual_version


__all__ = [
    "ConcurrencyError",
    "CursorFilterMismatchError",
    "CursorInvalidError",
]
