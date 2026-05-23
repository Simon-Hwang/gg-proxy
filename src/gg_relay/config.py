"""Process-wide configuration backed by pydantic-settings.

All env vars are prefixed with ``RELAY_``; secrets are typed as
:class:`SecretStr` so accidental logging shows ``**********`` instead of
the raw value.

The ``check-secrets`` CLI command validates that the required-for-production
fields are present and fails fast if not.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("gg_relay.config")


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


# Plan 7 D7.26 — single-team multi-maintainer collaboration label
# parser. Each comma-separated token in ``RELAY_API_KEYS_RAW`` is
# inspected with this regex to decide its shape:
#
#   * ``key:label``  — colon separator, both sides alphanum / dash /
#     underscore (regex anchored ``^...$``).
#   * ``label=key``  — equals separator, same character class.
#   * anything else  — whole token treated as a legacy bare key with
#     an auto-derived ``key-<sha256[:8]>`` label, so existing
#     ``RELAY_API_KEYS_RAW="k1,k2"`` deployments keep working silently.
#
# The character class explicitly includes ``-`` so the common SDK key
# shape ``sk-abc-123`` participates in the ``key:label`` /
# ``label=key`` paths instead of degrading to the whole-token
# fallback. Tokens with multiple separators (``key:val:extra``) or
# other special chars (``k/e/y``) intentionally fall through to the
# whole-token path — silently splitting on the first separator would
# be a footgun for operators who include unusual chars in their keys.
_LABEL_TOKEN = re.compile(r"^([A-Za-z0-9_-]+)([:=])([A-Za-z0-9_-]+)$")


def _parse_keys_with_labels(raw: str) -> dict[str, str]:
    """Parse ``RELAY_API_KEYS_RAW`` into ``{key: label}``.

    Per-token format detection (anchored regex match):

      * ``"abc:label"``  → key=``abc`` label=``label`` (``:`` separator)
      * ``"label=abc"``  → key=``abc`` label=``label`` (``=`` separator)
      * anything else    → whole token is the key, label is
        ``"key-<sha256(key)[:8]>"`` (legacy-safe — pre-D7.26 callers
        that never set a label still get a stable identifier).

    Returns ``dict[key → label]``. When two tokens share a label but
    map to different keys we ``logger.warning(...)`` and the **later**
    token wins (deterministic last-wins semantics so a redeploy with
    swapped tokens produces a predictable state).
    """
    result: dict[str, str] = {}
    seen_labels: dict[str, str] = {}
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        m = _LABEL_TOKEN.match(tok)
        if m:
            first, sep, second = m.group(1), m.group(2), m.group(3)
            if sep == ":":
                k, lbl = first, second
            else:
                lbl, k = first, second
        else:
            k = tok
            lbl = f"key-{hashlib.sha256(tok.encode()).hexdigest()[:8]}"
        if lbl in seen_labels and seen_labels[lbl] != k:
            logger.warning(
                "api_key label %r is shared by multiple keys; "
                "later token wins",
                lbl,
            )
        result[k] = lbl
        seen_labels[lbl] = k
    return result


class Config(BaseSettings):
    """Settings sourced from env (``RELAY_*``) + optional ``.env`` file.

    Convention: secrets use ``SecretStr``; paths use ``Path``; CSV-shaped
    list env vars stay as ``str`` and are exposed via ``@property`` so we
    avoid pydantic-settings' default JSON parsing of list fields.
    """

    model_config = SettingsConfigDict(
        env_prefix="RELAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── core API ────────────────────────────────────────────────────────
    api_keys_raw: str = ""
    """Comma-separated API keys (``RELAY_API_KEYS=k1,k2,k3``).

    Plan 7 D7.26 — accepts three per-token shapes for single-team
    multi-maintainer collaboration::

      * ``key`` (legacy bare key — auto-derives ``key-<sha256[:8]>``
        label so old env vars keep working without warnings)
      * ``key:label`` (colon separator)
      * ``label=key`` (equals separator)

    Stored as a string because pydantic-settings would otherwise try to
    JSON-decode a list value out of the env var (and fail on plain CSV).
    """

    @property
    def api_keys_with_labels(self) -> dict[str, str]:
        """Plan 7 D7.26 — parsed view of :attr:`api_keys_raw` as
        ``{key: label}``. The label is what
        :class:`APIKeyAuthMiddleware` writes to
        ``request.state.api_key_label`` so the session router can
        auto-attribute ``sessions.owner`` without the client having
        to pass it explicitly.
        """
        return _parse_keys_with_labels(self.api_keys_raw)

    @property
    def api_keys(self) -> set[str]:
        """Legacy set-of-keys view (no labels).

        Plan 7 D7.26 keeps this property so callers that only need
        the key set (CLI ``check-secrets`` / ``status`` / tests that
        assert "is some key configured") don't have to learn the
        labelled dict shape. Use :attr:`api_keys_with_labels` when
        the label matters (middleware wiring, owner attribution).
        """
        return set(self.api_keys_with_labels.keys())

    public_base_url: str = ""
    """Externally-reachable base URL (used by IM card callbacks)."""

    # ── persistence ─────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./relay.db"

    # ── plugin assembler ────────────────────────────────────────────────
    gg_plugins_home: Path = Path("/opt/gg-plugins")
    install_dir_root: Path = Path("/var/lib/gg-relay/installs")

    # ── docker backend ──────────────────────────────────────────────────
    docker_image: str = "ghcr.io/gg-org/gg-relay-runner:latest"
    docker_socket_root: Path = Path("/var/run/gg-relay")

    # ── proxy ───────────────────────────────────────────────────────────
    outbound_proxy_url: str | None = None
    """If unset the lifespan starts the built-in MinimalProxy."""

    proxy_port: int = 8888
    proxy_audit_log: Path = Path("/var/log/gg-relay/proxy-audit.jsonl")

    # ── session manager ─────────────────────────────────────────────────
    default_timeout_s: int = 1800
    max_concurrent: int = 10
    grace_period_s: int = 30

    # ── pause/resume (Plan 6) ──────────────────────────────────────────
    paused_timeout_s: int = 1800
    """How long a session may stay PAUSED before the watchdog cancels it
    with ``end_reason='paused_timeout'`` (Plan 6 D6.2 / §10 risk row).
    30 minutes by default — same as :attr:`default_timeout_s`."""

    max_paused: int = 50
    """Global cap on simultaneously-PAUSED sessions (Plan 6 D6.17).
    Exceeding it raises :class:`MaxPausedExceeded` (HTTP 429)."""

    max_paused_per_api_key: int = 20
    """Per-X-API-Key cap on simultaneously-PAUSED sessions (D6.17)."""

    resume_timeout_s: float = 60.0
    """How long :meth:`SessionManager.resume` waits to re-acquire the
    semaphore slot before raising :class:`ResumeQueueTimeout` (D6.2 /
    §10 risk row). Routes map this to HTTP 429 + Retry-After."""

    # ── rate limiting (Plan 7 Task 10 / D7.7+D7.8) ─────────────────────
    rate_limit_enabled: bool = True
    """Master switch for :class:`RateLimitMiddleware`. When ``False`` the
    middleware is not wired, so per-request bucket logic is skipped
    entirely. Plan 8 D8.2 will swap the in-process limiter for a
    Redis-backed one; the flag stays so deployments can disable rate
    limiting if a fronting proxy already provides it."""

    rate_limit_per_min: int = 60
    """Token-bucket refill rate in tokens-per-minute, per API key id.
    Default 60/min ≈ 1 rps which matches typical Anthropic-tier quotas."""

    rate_limit_burst: int = 60
    """Maximum tokens a single bucket can hold (= initial allowance for
    a fresh key). Defaults match :attr:`rate_limit_per_min`, so a fresh
    key can spend its full minute of allowance immediately, then refills
    smoothly."""

    rate_limit_lru_cap: int = 10_000
    """Hard cap on how many distinct buckets the in-process limiter
    keeps in memory. When exceeded the LRU bucket is evicted (and its
    matching ``asyncio.Lock`` released) to bound memory use."""

    rate_limit_ttl_s: int = 3600
    """How long an idle bucket survives before the periodic sweeper
    drops it (and the matching lock). Default 1 hour — chosen so a key
    that goes silent recovers a full burst on its next request."""

    max_concurrent_sessions: int | None = None
    """Plan-6-only alias for :attr:`max_concurrent`; preserved so the
    config can be tuned independently for tests that need to drive
    pause/resume contention without forcing the production default."""

    # ── OTel ────────────────────────────────────────────────────────────
    otel_endpoint: str | None = None
    otel_exporter: Literal["grpc", "http", "console"] = "grpc"

    # ── IM (Feishu only in Plan 4) ──────────────────────────────────────
    feishu_app_id: SecretStr | None = None
    feishu_app_secret: SecretStr | None = None
    feishu_webhook_secret: SecretStr | None = None
    feishu_target_chat_id: str | None = None

    # ── redaction ───────────────────────────────────────────────────────
    redaction_patterns_raw: str = ""
    """Extra regex patterns (CSV) appended to RedactionEngine defaults."""

    redaction_keys_raw: str = ""
    """Extra dict-key names (CSV) treated as sensitive."""

    @property
    def redaction_patterns(self) -> list[str]:
        return _split_csv(self.redaction_patterns_raw)

    @property
    def redaction_keys(self) -> list[str]:
        return _split_csv(self.redaction_keys_raw)

    # ── dashboard ──────────────────────────────────────────────────────
    dashboard_admin_password: SecretStr | None = None
    dashboard_session_secret: SecretStr | None = None

    # ── dashboard (Plan 6) ─────────────────────────────────────────────
    chart_js_cdn: str = (
        "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"
    )
    """D6.5=A: CDN URL for Chart.js. jsdelivr by default. Override to
    point at any other CDN (unpkg, etc.) or a locally vendored path
    when ``chart_js_offline=True``."""

    chart_js_offline: bool = False
    """When True the Kanban / detail templates load Chart.js from
    ``chart_js_cdn`` only if it resolves to a path under
    ``/dashboard/static/``. Operators serving the dashboard from an
    air-gapped network set ``chart_js_offline=True`` AND vendor the
    js file under ``src/gg_relay/dashboard/static/vendor/chart.umd.min.js``
    (or wherever ``chart_js_cdn`` points)."""

    kanban_default_page_size: int = 50
    """D6.16: how many cards a single kanban-board page renders before
    HTMX's ``hx-trigger='revealed'`` fires the next-page load."""

    jaeger_ui_url: str | None = None
    """D6.6=A + D6.14: external Jaeger UI base URL used by the
    per-session ``span_tree.html`` partial. Two recommended values:

      * Production: ``"/jaeger"`` (same-origin via the nginx reverse
        proxy in ``deploy/nginx/jaeger-proxy.conf`` — keeps the iframe
        free of ``X-Frame-Options`` denials).
      * Development: ``"http://localhost:16686"`` direct to the local
        Jaeger UI container.

    Leave unset to disable the iframe entirely; the template falls
    back to a plain trace-id readout with a disabled ``Open in Jaeger``
    button (D6.6 fallback)."""

    # ── integrations ───────────────────────────────────────────────────
    task_trace_path: Path | None = Path("~/.claude/metrics/gg-task-trace.jsonl")
    """Where the :class:`TaskTraceSubscriber` writes ``gg.task-trace.v1``
    JSONL records (D5.7=A + D5.16). ``None`` disables the writer entirely.

    The default value targets the gg-plugins integration path; production
    deployments running multiple gg-relay instances on the same host
    MUST set a host-unique path (or ``None``) per the deployment guide,
    otherwise concurrent line writes will interleave."""

    @property
    def task_trace_path_resolved(self) -> Path | None:
        """Expand ``~`` in :attr:`task_trace_path` so callers don't have to."""
        if self.task_trace_path is None:
            return None
        return Path(self.task_trace_path).expanduser()


REQUIRED_FOR_PROD: tuple[str, ...] = (
    "api_keys",
    "public_base_url",
    "dashboard_admin_password",
    "dashboard_session_secret",
)
"""Field/property names enforced by ``gg-relay check-secrets``.

Intentionally small — production deployments choose which optional backends
(docker, OTel, Feishu) they use; only the universally required wiring is
checked here. ``api_keys`` is a property that materialises the parsed
list, so the check works regardless of whether the env var was set as
``RELAY_API_KEYS_RAW`` (canonical) or whether the operator constructed the
Config object in Python.
"""


def missing_required(cfg: Config) -> list[str]:
    """Return the names of REQUIRED_FOR_PROD fields that are empty/None."""
    missing: list[str] = []
    for name in REQUIRED_FOR_PROD:
        value = getattr(cfg, name)
        if _is_empty(value):
            missing.append(name)
    return missing


def _is_empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list | set | dict):
        return not value
    if isinstance(value, SecretStr):
        return not value.get_secret_value()
    return False
