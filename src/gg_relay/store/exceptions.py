"""Store-layer exceptions — Plan 7 Task 9 (D7.6).

Cursor-pagination errors raised by :meth:`SqlAlchemyStore.list_sessions`
when a client-supplied ``after=`` cursor is malformed or no longer
matches the active filter combination.

Both inherit from :class:`ValueError` so unwary callers that catch the
broad base class still see them as input-validation failures; the
FastAPI router catches the specific subclasses and maps them to ``400``
with a discriminating ``code`` field.
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


__all__ = ["CursorFilterMismatchError", "CursorInvalidError"]
