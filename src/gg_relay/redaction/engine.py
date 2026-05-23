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

Plan 7 Task 11 (D7.15) adds two extras for the structlog pipeline used by
the API layer:

* :data:`SENSITIVE_PATTERN` — a compact regex tuned for ``event_dict``
  *values* (api_key=…, password=…, bearer X, token: X). It's deliberately
  narrower than :data:`DEFAULT_PATTERNS` because event-dict values are
  often short strings where over-aggressive pattern matching produced
  too many false positives in earlier drafts.
* :func:`redaction_processor` — a structlog processor that masks
  :class:`pydantic.SecretStr` values + any string that triggers
  :data:`SENSITIVE_PATTERN`. Wired into ``structlog.configure`` from
  :func:`gg_relay.api.main.lifespan` so log records emitted by routes,
  middleware, and the session manager get masked at source.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import SecretStr

REDACTED = "***REDACTED***"
"""The single placeholder string emitted in place of any redacted value."""

# Plan 7 Task 11 (D7.15) — short-string sensitive pattern used by the
# structlog processor. Tighter than :data:`DEFAULT_PATTERNS` because
# event-dict values are short and easily false-positive on broader
# regexes. Matches:
#   * ``api_key=<token>`` / ``api-key: <token>`` / ``apikey=<token>``
#   * ``password=<token>`` / ``password: <token>``
#   * ``token=<token>``    / ``token: <token>``
#   * ``secret=<token>``   / ``secret: <token>``
#   * ``Bearer <token>``   (case-insensitive)
# Each pattern requires at least one non-space character after the
# separator so bare words like "api_key" in a sentence don't trigger.
SENSITIVE_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)("
    r"(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+"
    r"|bearer\s+\S+"
    r")"
)


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


# ── Plan 7 Task 11 (D7.15) — structlog processor ─────────────────────


def _mask_value(value: Any) -> Any:
    """Mask a single value if it looks sensitive.

    Three rules:
      1. :class:`pydantic.SecretStr` → ``"***"`` (the only reliable way
         to detect a secret-typed config field once it's serialised
         into a log record).
      2. ``str`` matching :data:`SENSITIVE_PATTERN` → ``"***"`` (catches
         operator mistakes like logging ``f"using api_key={cfg.k}"``).
      3. Everything else (int, bool, None, dict, list, …) is returned
         unchanged — recursive redaction belongs in
         :class:`RedactionEngine`; this helper is the lightweight
         per-value masker that the structlog processor uses on every
         event_dict value.

    Returns the placeholder string ``"***"`` (not the longer
    :data:`REDACTED`) so structured log output stays compact and
    grep-friendly.
    """
    if isinstance(value, SecretStr):
        return "***"
    if isinstance(value, str) and SENSITIVE_PATTERN.search(value):
        return "***"
    return value


def redaction_processor(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """structlog processor masking :class:`SecretStr` + sensitive strings.

    Signature follows the structlog processor protocol
    (``(logger, method_name, event_dict) -> event_dict``). Wired in
    :func:`gg_relay.api.main.lifespan` as the **first** processor so
    later processors (JSON renderer, console renderer, …) never see
    plaintext secrets.

    The processor only inspects top-level event_dict values; nested
    dicts/lists are NOT recursed because structlog event dicts are
    intentionally shallow. Callers passing complex objects should
    pre-mask them via :class:`RedactionEngine.redact_value`.
    """
    del logger, method_name  # processor protocol args, unused
    return {k: _mask_value(v) for k, v in event_dict.items()}
