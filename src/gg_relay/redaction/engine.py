"""Regex + key-based redaction engine (Plan 4 D4.12, P0).

Every dict/string written to the persistence layer or to dashboards must
pass through this engine first. The rules cover two orthogonal layers:

1. **Key-based masking**: keys whose case-insensitive name appears in
   :data:`DEFAULT_SENSITIVE_KEYS` (api_key, token, secret, password, …)
   are blanked unconditionally, regardless of the value's shape.

2. **Pattern-based masking**: strings are scanned for known secret formats
   (Anthropic ``sk-ant-…``, generic ``api_key=…``, ``Bearer …``, AWS access
   IDs). Matches are replaced with :data:`REDACTED`.

The engine is *intentionally* over-aggressive on key names — false positives
are cheap (one redacted log line); false negatives leak credentials.
Custom patterns can be supplied via :class:`RedactionEngine` constructor.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "***REDACTED***"
"""The single placeholder string emitted in place of any redacted value."""


DEFAULT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ``api_key=value`` / ``api-key: value`` / ``token = "value"`` / …
    re.compile(
        r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*['\"]?"
        r"([\w\-\.\+/]+)['\"]?"
    ),
    # Anthropic API key shape.
    re.compile(r"sk-ant-[\w\-]+"),
    # Authorization: Bearer <jwt-or-token>.
    re.compile(r"(?i)bearer\s+[\w\-\.]+"),
    # AWS Access Key ID format (always 20 chars after AKIA).
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # GitHub personal-access token (ghp_ / gho_ / ghu_ / ghs_ + 36 chars).
    re.compile(r"gh[posu]_[A-Za-z0-9]{36,}"),
)

DEFAULT_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "api-key",
        "token",
        "secret",
        "password",
        "pass",
        "passwd",
        "credentials",
        "auth",
        "authorization",
        "anthropic_api_key",
        "openai_api_key",
        "x-api-key",
    }
)


class RedactionEngine:
    """Apply regex- and key-based masking to strings / dicts / frames.

    Construction is cheap; engines can be cached at the module scope. The
    methods are pure (no mutation of input) — the redacted copy is always a
    new object so the caller can keep the raw values for in-memory use
    (e.g. injecting credentials into a container env).
    """

    def __init__(
        self,
        *,
        patterns: Iterable[re.Pattern[str]] = DEFAULT_PATTERNS,
        sensitive_keys: Iterable[str] = DEFAULT_SENSITIVE_KEYS,
    ) -> None:
        self._patterns: tuple[re.Pattern[str], ...] = tuple(patterns)
        # Normalise keys to lowercase once at construction time — the hot
        # path then does a single dict lookup per key.
        self._keys: frozenset[str] = frozenset(k.lower() for k in sensitive_keys)

    @property
    def sensitive_keys(self) -> frozenset[str]:
        """Read-only view of the configured sensitive-key set."""
        return self._keys

    def redact_string(self, s: str) -> str:
        """Mask every regex-matching substring in ``s``."""
        out = s
        for pattern in self._patterns:
            out = pattern.sub(REDACTED, out)
        return out

    def redact_value(self, value: Any) -> Any:
        """Recursive redaction of an arbitrary JSON-ish value."""
        if isinstance(value, str):
            return self.redact_string(value)
        if isinstance(value, Mapping):
            return self.redact_dict(dict(value))
        if isinstance(value, list):
            return [self.redact_value(v) for v in value]
        if isinstance(value, tuple):
            # Tuples become lists — JSON columns don't preserve tuples
            # anyway, so normalise here.
            return [self.redact_value(v) for v in value]
        return value

    def redact_dict(self, d: Mapping[str, Any]) -> dict[str, Any]:
        """Return a shallow-defensive-copy with keys + values masked.

        - Keys whose lowercase form is in :attr:`sensitive_keys` get their
          value replaced with :data:`REDACTED` regardless of type.
        - Nested dicts / lists are traversed recursively.
        - Plain strings are run through :meth:`redact_string`.
        - Other types (int, bool, None, datetime) pass through unchanged.
        """
        out: dict[str, Any] = {}
        for k, v in d.items():
            if isinstance(k, str) and k.lower() in self._keys:
                out[k] = REDACTED
                continue
            out[k] = self.redact_value(v)
        return out

    def redact_frame(self, frame: Mapping[str, Any]) -> dict[str, Any]:
        """Frame-shaped wrapper around :meth:`redact_dict`.

        Currently identical to :meth:`redact_dict` — kept as a separate name
        so future per-frame-type rules (e.g. preserve seq/ts even if the
        key matches "ts" by accident) can be added without churning callers.
        """
        return self.redact_dict(frame)
