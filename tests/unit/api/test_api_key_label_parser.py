"""Plan 7 Task 6b / D7.26 — :func:`_parse_keys_with_labels`.

The parser is the per-token dispatch responsible for turning the
``RELAY_API_KEYS_RAW`` env var into the ``{key: label}`` dict that
:class:`APIKeyAuthMiddleware` consumes. Three shapes per token are
accepted with anchored regex matching:

  * ``"key:label"``  → ``{key: label}``
  * ``"label=key"``  → ``{key: label}``
  * anything else    → whole token kept as key, label auto-derived
    from ``"key-<sha256[:8]>"`` so existing bare-key deployments
    keep working silently.
"""
from __future__ import annotations

import hashlib
import logging

from gg_relay.config import _parse_keys_with_labels


def _auto_label(key: str) -> str:
    """Mirror the parser's auto-label scheme so test expectations stay
    in lock-step with the implementation."""
    return f"key-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


class TestParseLegacy:
    def test_parse_legacy_bare_key(self) -> None:
        """Plain ``"sk-abc-123"`` — NOT ``key:label`` shape because
        the auto-label fallback path is exercised only when the regex
        does NOT match. ``sk-abc-123`` contains dashes only — no
        ``:``/``=`` separator — so it falls through to the bare-key
        path with an auto-derived hash label."""
        out = _parse_keys_with_labels("sk-abc-123")
        assert out == {"sk-abc-123": _auto_label("sk-abc-123")}

    def test_parse_legacy_csv_silent(self) -> None:
        """``RELAY_API_KEYS_RAW="key1,key2"`` (the pre-D7.26 shape)
        must produce no warnings and yield both keys with auto-derived
        labels (different labels — sha256 prefixes don't collide for
        these inputs)."""
        out = _parse_keys_with_labels("key1,key2")
        assert out == {
            "key1": _auto_label("key1"),
            "key2": _auto_label("key2"),
        }
        assert len(set(out.values())) == 2

    def test_parse_legacy_empty_returns_empty_dict(self) -> None:
        assert _parse_keys_with_labels("") == {}

    def test_parse_trims_whitespace_and_skips_blanks(self) -> None:
        out = _parse_keys_with_labels(" k1 , , k2 ")
        assert out == {
            "k1": _auto_label("k1"),
            "k2": _auto_label("k2"),
        }


class TestParseLabelled:
    def test_parse_key_colon_label_simple(self) -> None:
        out = _parse_keys_with_labels("abc:alice")
        assert out == {"abc": "alice"}

    def test_parse_key_colon_label_with_dashes(self) -> None:
        """The regex char class is ``[A-Za-z0-9_-]+`` so dash-bearing
        SDK-style keys ``sk-abc-123`` participate in the
        ``key:label`` path (instead of degrading to the auto-label
        fallback)."""
        out = _parse_keys_with_labels("sk-abc-123:alice")
        assert out == {"sk-abc-123": "alice"}

    def test_parse_label_eq_key(self) -> None:
        out = _parse_keys_with_labels("alice=sk-abc-123")
        assert out == {"sk-abc-123": "alice"}

    def test_parse_mixed_shapes(self) -> None:
        """All three shapes can coexist in one env var; ordering is
        preserved in the dict (CPython 3.7+ semantics)."""
        out = _parse_keys_with_labels(
            "bare1,alice=k1,k2:bob,bare2"
        )
        assert out == {
            "bare1": _auto_label("bare1"),
            "k1": "alice",
            "k2": "bob",
            "bare2": _auto_label("bare2"),
        }


class TestParseFallback:
    def test_parse_multi_colon_kept_whole(self) -> None:
        """``"k:v:extra"`` does not match the anchored regex (two
        ``:`` separators), so the whole string becomes the key with
        an auto-derived label."""
        tok = "key:val:extra"
        out = _parse_keys_with_labels(tok)
        assert out == {tok: _auto_label(tok)}

    def test_parse_special_chars_kept_whole(self) -> None:
        """``/`` is not in ``[A-Za-z0-9_-]+`` so ``"k/e/y"`` falls
        through to the whole-token path."""
        tok = "k/e/y"
        out = _parse_keys_with_labels(tok)
        assert out == {tok: _auto_label(tok)}

    def test_parse_dot_kept_whole(self) -> None:
        """Dots also outside the char class — defensive coverage so a
        relax of the regex would surface here first."""
        tok = "k.e.y"
        out = _parse_keys_with_labels(tok)
        assert out == {tok: _auto_label(tok)}


class TestDuplicateLabels:
    def test_parse_duplicate_label_warns_last_wins(
        self, caplog: logging.LogRecord
    ) -> None:
        """Two ``key:label`` tokens with the same label but different
        keys must log a warning and keep both keys in the dict — the
        warning is operator-visible so the redeploy can be fixed,
        and last-wins is deterministic across restarts."""
        with caplog.at_level(logging.WARNING, logger="gg_relay.config"):
            out = _parse_keys_with_labels("k1:alice,k2:alice")
        assert out == {"k1": "alice", "k2": "alice"}
        assert any(
            "alice" in rec.getMessage() and "label" in rec.getMessage()
            for rec in caplog.records
        )

    def test_parse_duplicate_label_no_warn_when_same_key(self) -> None:
        """``"k1:alice,k1:alice"`` is benign (same key + same label
        twice) — no warning, just deduplicated to one entry."""
        out = _parse_keys_with_labels("k1:alice,k1:alice")
        assert out == {"k1": "alice"}
