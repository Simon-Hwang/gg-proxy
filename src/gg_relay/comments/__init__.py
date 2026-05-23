"""Session comments package (Plan 8 D8.5 / Task 7).

Public surface:

* :func:`render_safe` — markdown → sanitized HTML pipeline used by
  :mod:`gg_relay.api.routers.comments` on every create / update. The
  router stores both the raw markdown and the pre-sanitized HTML so
  the dashboard renders without re-running ``bleach`` per page-view.
"""
from gg_relay.comments.sanitizer import (
    ALLOWED_ATTRS,
    ALLOWED_PROTOCOLS,
    ALLOWED_TAGS,
    render_safe,
)

__all__ = [
    "ALLOWED_ATTRS",
    "ALLOWED_PROTOCOLS",
    "ALLOWED_TAGS",
    "render_safe",
]
