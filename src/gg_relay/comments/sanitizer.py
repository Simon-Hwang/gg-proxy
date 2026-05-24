"""Comment markdown → sanitized HTML (Plan 8 D8.5 / Task 7).

Two-stage pipeline:

  1. :class:`markdown_it.MarkdownIt` parses CommonMark and renders to
     HTML. CommonMark mode is deliberately chosen over GFM-extras
     because every additional rule (e.g. raw inline HTML pass-through)
     widens the attack surface that the sanitiser then has to
     re-narrow.
  2. :func:`bleach.clean` allow-lists tags / attributes / link
     protocols. Anything outside the allow-list is **stripped** (not
     escaped — ``strip=True``) so a payload like
     ``<script>alert(1)</script>`` disappears entirely rather than
     surviving as literal angle-bracket text the user would see.

Allowed surface:

  * Block tags: ``p``, ``pre``, ``blockquote``, ``ul``, ``ol``, ``li``,
    ``h1``–``h4``.
  * Inline tags: ``br``, ``code``, ``strong``, ``em``, ``a``.
  * Attributes: ``a[href]`` and ``a[title]`` only. Every other
    attribute (``onerror``, ``onload``, ``style``, ``id``, …) is
    stripped — image elements are not even in the allow-list, so the
    classic ``<img src=x onerror=alert(1)>`` payload is removed at
    the tag level before the attribute filter runs.
  * Link protocols: ``http``, ``https``, ``mailto``. ``javascript:``,
    ``data:``, ``vbscript:``, and bare relative URLs all fail the
    protocol check and bleach removes the ``href`` attribute (the
    surrounding ``<a>`` tag survives without a target).

XSS payloads covered by the unit tests in
``tests/unit/comments/test_sanitizer.py``:

  * ``<script>alert(1)</script>`` — tag stripped (``<script>`` not in
    allow-list).
  * ``<img src=x onerror=alert(1)>`` — tag stripped.
  * ``[click](javascript:alert(1))`` — markdown produces
    ``<a href="javascript:alert(1)">``; bleach drops the disallowed
    protocol.
  * ``[click](https://example.com)`` — href survives intact.
  * ``**bold** *em* `code` `` — basic markdown round-trips.
"""
from __future__ import annotations

import bleach  # type: ignore[import-untyped]
from markdown_it import MarkdownIt

# Block-level structure plus the few inline tags worth supporting.
# Image tags are intentionally absent — Plan 8 Task 7's MVP is
# text-only comments; an image upload pipeline would need its own
# storage + scanning policy.
ALLOWED_TAGS: list[str] = [
    "p",
    "br",
    "code",
    "pre",
    "strong",
    "em",
    "a",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "blockquote",
]

# Only ``<a>`` keeps attributes, and only ``href`` + ``title``. Every
# other attribute (including ``target``, ``rel``, ``onclick``, …) is
# stripped. The router can layer ``rel="nofollow noopener"`` post-hoc
# if a future requirement needs it; doing it here would couple the
# sanitiser to URL classification logic that doesn't belong at this
# layer.
ALLOWED_ATTRS: dict[str, list[str]] = {"a": ["href", "title"]}

# Restricting to http / https / mailto blocks every URI-borne XSS
# vector (``javascript:``, ``data:``, ``vbscript:``, ``file:``,
# custom-scheme bridges to native apps, …). Bare relative URLs are
# also rejected by bleach when this allow-list is in place — that's
# acceptable for comments which always reference absolute upstream
# resources.
ALLOWED_PROTOCOLS: list[str] = ["http", "https", "mailto"]

# Process-wide CommonMark renderer. ``MarkdownIt`` is thread-safe for
# concurrent renders (no mutable state between calls) so a single
# module-level instance keeps the per-request render cheap.
_md = MarkdownIt("commonmark")


def render_safe(markdown_text: str) -> str:
    """Render markdown to sanitised HTML.

    Two-stage: CommonMark render → ``bleach.clean(strip=True)``.
    Disallowed tags (``<script>``, ``<img>``, raw ``<style>``, …)
    are removed entirely rather than escaped, so the rendered output
    never carries literal ``&lt;script&gt;`` text the user would
    see. Disallowed protocols on ``<a href=...>`` cause the ``href``
    attribute to be dropped — the surrounding ``<a>`` tag survives
    without a navigable target.
    """
    raw_html = _md.render(markdown_text)
    cleaned: str = bleach.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    return cleaned


__all__ = [
    "ALLOWED_ATTRS",
    "ALLOWED_PROTOCOLS",
    "ALLOWED_TAGS",
    "render_safe",
]
