"""Markdown → HTML sanitiser unit tests — Plan 8 D8.5 / Task 7.

Pins the XSS allow-list contract enforced by
:func:`gg_relay.comments.sanitizer.render_safe`. Each test exercises
a single attack class so a regression (e.g. someone widens
``ALLOWED_TAGS`` to include ``img`` without re-thinking the
``onerror`` attribute) lands on a focused failure.

Covered surface (mirrors module docstring):

  * ``<script>`` tag stripped entirely.
  * ``<img onerror=...>`` tag stripped entirely (img not in allow-list).
  * ``[click](javascript:alert(1))`` — markdown produces an ``<a>``
    with a ``javascript:`` href; bleach drops the disallowed protocol.
  * ``[click](https://example.com)`` — http(s) href passes through.
  * Basic markdown (``**bold**`` / ``*em*`` / `` `code` ``) round-trips.
"""
from __future__ import annotations

from gg_relay.comments.sanitizer import render_safe


class TestScriptTagStripped:
    """``<script>`` payloads must be removed entirely.

    ``bleach.clean(strip=True)`` removes the script tag itself; the
    text content inside (e.g. ``alert(1)``) survives as literal
    paragraph text after the tag is unwrapped, which is harmless
    (no JS execution context) and what bleach's ``strip`` does by
    design. The security guarantee we pin here is that no
    ``<script>`` *element* survives — the literal text leaking
    through as inert paragraph copy is acceptable and tested
    separately so a future "also strip script content" upgrade
    doesn't break the assertion silently.
    """

    def test_inline_script_tag_stripped(self) -> None:
        html = render_safe("<script>alert(1)</script>")
        assert "<script" not in html.lower()
        assert "</script" not in html.lower()

    def test_script_tag_in_paragraph_stripped(self) -> None:
        html = render_safe("hello <script>alert(1)</script> world")
        assert "<script" not in html.lower()
        # The surrounding paragraph text must survive.
        assert "hello" in html
        assert "world" in html


class TestImgOnerrorStripped:
    """``<img>`` is not in :data:`ALLOWED_TAGS`, so every ``<img>``
    variant (including the classic ``onerror`` payload and the
    ``onload`` variant) is removed entirely."""

    def test_img_onerror_stripped(self) -> None:
        html = render_safe('<img src=x onerror=alert(1)>')
        assert "<img" not in html.lower()
        assert "onerror" not in html.lower()

    def test_img_onload_stripped(self) -> None:
        html = render_safe('<img src="x" onload="alert(1)">')
        assert "<img" not in html.lower()
        assert "onload" not in html.lower()


class TestAnchorProtocolAllowList:
    """``<a href=...>`` survives only for http / https / mailto.

    Test inputs use raw HTML ``<a href="...">`` rather than the
    markdown ``[text](url)`` form because ``markdown_it`` runs its
    own validator that REJECTS dangerous protocols (``javascript:``,
    ``data:``, ``vbscript:``) at parse time — the markdown link
    never produces an ``<a>`` element to begin with. Using raw HTML
    in the payload ensures the dangerous href reaches the bleach
    stage where the protocol allow-list is the defence in depth.
    """

    def test_javascript_protocol_href_dropped(self) -> None:
        """Raw ``<a href="javascript:alert(1)">`` — bleach removes
        the ``href`` attribute when the protocol is not allow-listed.
        The surrounding ``<a>`` tag may survive but must NOT carry
        a navigable ``href`` to a ``javascript:`` URL.
        """
        html = render_safe('<a href="javascript:alert(1)">click</a>')
        # No live href targeting javascript:.
        assert 'href="javascript:' not in html.lower()
        assert "href='javascript:" not in html.lower()

    def test_data_protocol_href_dropped(self) -> None:
        """``data:`` URIs are an XSS vector via ``data:text/html`` —
        explicitly NOT allow-listed."""
        html = render_safe(
            '<a href="data:text/html,<script>alert(1)</script>">x</a>'
        )
        # No live href targeting data:.
        assert 'href="data:' not in html.lower()
        # No script tag survived either (defence in depth).
        assert "<script" not in html.lower()

    def test_https_protocol_href_kept(self) -> None:
        """Sanity: a benign ``https://`` link survives intact."""
        html = render_safe("[click](https://example.com)")
        # The href attribute should be present with the original URL.
        assert "https://example.com" in html
        assert "<a " in html
        # Anchor text survives.
        assert ">click</a>" in html

    def test_http_protocol_href_kept(self) -> None:
        html = render_safe("[link](http://example.com/path)")
        assert "http://example.com/path" in html

    def test_mailto_protocol_href_kept(self) -> None:
        html = render_safe("[email](mailto:user@example.com)")
        assert "mailto:user@example.com" in html


class TestMarkdownBasics:
    """Sanity tests so a future allow-list narrowing that breaks
    common-case rendering surfaces immediately."""

    def test_bold_renders(self) -> None:
        html = render_safe("**bold text**")
        assert "<strong>bold text</strong>" in html

    def test_em_renders(self) -> None:
        html = render_safe("*emphasized*")
        assert "<em>emphasized</em>" in html

    def test_inline_code_renders(self) -> None:
        html = render_safe("`code`")
        assert "<code>code</code>" in html

    def test_paragraph_renders(self) -> None:
        html = render_safe("just a paragraph")
        assert "<p>just a paragraph</p>" in html

    def test_list_renders(self) -> None:
        html = render_safe("- one\n- two\n")
        assert "<ul>" in html
        assert "<li>one</li>" in html
        assert "<li>two</li>" in html

    def test_headings_h1_through_h4_render(self) -> None:
        html = render_safe("# h1\n\n## h2\n\n### h3\n\n#### h4\n")
        assert "<h1>h1</h1>" in html
        assert "<h2>h2</h2>" in html
        assert "<h3>h3</h3>" in html
        assert "<h4>h4</h4>" in html

    def test_blockquote_renders(self) -> None:
        html = render_safe("> quoted\n")
        assert "<blockquote>" in html
        assert "quoted" in html


class TestEntitySmuggling:
    """Encoded payloads must not slip through as live HTML.

    A naive sanitiser that runs decode → re-encode could turn
    ``&lt;script&gt;`` back into ``<script>``. ``bleach`` operates
    on the rendered tree, so the entity stays literal text.
    """

    def test_html_entity_script_stays_literal(self) -> None:
        html = render_safe("&lt;script&gt;alert(1)&lt;/script&gt;")
        # No live script tag.
        assert "<script" not in html.lower()
