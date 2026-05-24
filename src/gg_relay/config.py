"""Process-wide configuration backed by pydantic-settings.

All env vars are prefixed with ``RELAY_``; secrets are typed as
:class:`SecretStr` so accidental logging shows ``**********`` instead of
the raw value.

The ``check-secrets`` CLI command validates that the required-for-production
fields are present and fails fast if not.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("gg_relay.config")

# Plan 7 Task 11 (D7.14) — fail-fast sentinel for the sqlite dev default.
# When :attr:`Config.database_url` still equals this string in production
# mode, :meth:`Config.validate_required_secrets` raises so the operator
# never accidentally ships a single-file sqlite as the production store.
DEFAULT_SQLITE_URL: str = "sqlite+aiosqlite:///./relay.db"


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


# ─────────────────────────────────────────────────────────────────────
# Plan 8 — env parsers (same shape as D7.26 ``_parse_keys_with_labels``)
#
# All four parsers share the same defensive contract: tolerate blank /
# malformed tokens, log a structured warning, drop the offending entry,
# and never raise. The motivation is that env-driven config is operator
# input; one fat-fingered token must not refuse the whole boot.
# ─────────────────────────────────────────────────────────────────────

_VALID_ROLES: tuple[str, ...] = ("viewer", "submitter", "admin")


def _parse_role_mapping(raw: str) -> dict[str, str]:
    """Parse ``RELAY_ROLE_MAPPING_RAW`` → ``{api_key_label: role}``.

    Format: ``"alice=admin,bob=submitter,carol=viewer"``. Tokens with
    an unknown role (anything outside :data:`_VALID_ROLES`) are logged
    via ``logger.warning(...)`` and dropped — the boot continues with
    the rest of the map so a single typo doesn't lock everyone out.

    Plan 8 D8.22 reads the returned dict at request-time to populate
    ``request.state.role`` (default ``"viewer"`` if the label is
    absent). Empty / missing env produces ``{}``; in production mode
    :meth:`Config.validate_required_secrets` emits an extra warning so
    operators notice that every key will fall back to ``viewer``.
    """
    result: dict[str, str] = {}
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok or "=" not in tok:
            continue
        label, role = tok.split("=", 1)
        label = label.strip()
        role = role.strip()
        if not label:
            continue
        if role not in _VALID_ROLES:
            logger.warning(
                "invalid role in RELAY_ROLE_MAPPING_RAW; dropping "
                "(label=%r role=%r valid=%s)",
                label,
                role,
                ",".join(_VALID_ROLES),
            )
            continue
        result[label] = role
    return result


def _parse_dashboard_users(raw: str) -> dict[str, str]:
    """Parse ``RELAY_DASHBOARD_USERS_RAW`` → ``{username: bcrypt_hash}``.

    Format: ``"alice=<bcrypt>,bob=<bcrypt>"``. The bcrypt hash shape
    (``$2b$<cost>$<22-char-salt><31-char-hash>``) uses ``[./A-Za-z0-9$]``
    only, so naive ``split(",")`` and ``split("=", 1)`` are safe even
    though bcrypt's char class includes ``$``. Tokens without ``=`` are
    skipped; an empty username drops the token.

    Plan 8 D8.26 — at lifespan boot, Task 3 reads this dict and
    derives an internal API key per username (``secrets.token_urlsafe(32)``)
    with label ``dashboard-<username>``. Empty dict = cookie auth
    disabled (the dashboard still works via the existing
    ``RELAY_DASHBOARD_ADMIN_PASSWORD`` flow), which is intentional for
    dev sandboxes.
    """
    result: dict[str, str] = {}
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok or "=" not in tok:
            continue
        username, bcrypt_hash = tok.split("=", 1)
        username = username.strip()
        bcrypt_hash = bcrypt_hash.strip()
        if not username or not bcrypt_hash:
            continue
        result[username] = bcrypt_hash
    return result


def _parse_feishu_user_mapping(raw: str) -> dict[str, str]:
    """Parse ``RELAY_FEISHU_USER_MAPPING_RAW`` → ``{api_key_label: open_id}``.

    Format: ``"alice=ou_xxx,bob=ou_yyy"``. Feishu ``open_id`` shape is
    ``[a-zA-Z0-9_]+`` (no commas, no equals) so the split is
    unambiguous. Plan 8 D8.7 ``AlertRouter`` reads this dict to build
    the ``mention_open_ids`` argument to
    :meth:`FeishuCardBuilder.build_alert_card`; an unmapped owner
    silently falls back to a non-@-mention card.
    """
    result: dict[str, str] = {}
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok or "=" not in tok:
            continue
        label, open_id = tok.split("=", 1)
        label = label.strip()
        open_id = open_id.strip()
        if not label or not open_id:
            continue
        result[label] = open_id
    return result


def _parse_alert_rules_json(raw: str) -> dict[str, list[str]]:
    """Parse ``RELAY_ALERT_RULES_JSON`` → ``{rule_name: [conditions]}``.

    Format: JSON object with string-list values, e.g.
    ``{"fail":["always"],"cancel":["timeout_recovered"],"complete":["tag=notify"]}``.
    Malformed JSON / wrong shape (non-dict, non-list values, non-str
    items) logs a warning and returns ``{}`` so the boot falls back to
    the default rules wired into :class:`AlertRouter` (fail always,
    cancel-timeout always, complete only when ``tag='notify'``).

    Plan 8 D8.7 — kept as JSON (rather than CSV) because a rule may
    legitimately carry multiple conditions and ``=`` already appears
    inside a condition body (``tag=notify``), so CSV+= would collide.
    """
    raw = raw.strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "RELAY_ALERT_RULES_JSON is not valid JSON; ignoring (err=%s)",
            exc,
        )
        return {}
    if not isinstance(payload, dict):
        logger.warning(
            "RELAY_ALERT_RULES_JSON must be a JSON object; ignoring "
            "(got %s)",
            type(payload).__name__,
        )
        return {}
    result: dict[str, list[str]] = {}
    for rule, conds in payload.items():
        if not isinstance(rule, str):
            continue
        if not isinstance(conds, list) or not all(
            isinstance(c, str) for c in conds
        ):
            logger.warning(
                "RELAY_ALERT_RULES_JSON rule %r conditions must be a "
                "list of strings; dropping",
                rule,
            )
            continue
        result[rule] = list(conds)
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
    database_url: str = DEFAULT_SQLITE_URL

    # ── Plan 7 Task 11 (D7.14) — fail-fast secret validation ───────────
    production_mode: bool = False
    """When True, :meth:`validate_required_secrets` raises
    :class:`RuntimeError` on any missing required secret (API keys,
    Feishu consistency, non-sqlite Postgres URL). When False (default,
    dev mode) it warns instead. Set via ``RELAY_PRODUCTION_MODE=true``
    in the lifespan boot env."""

    allow_no_keys: bool = False
    """Dev-only escape hatch — silences the dev-mode "no API keys"
    warning emitted by :meth:`validate_required_secrets`. Use only
    when running unit tests / a local sandbox that intentionally
    leaves ``RELAY_API_KEYS_RAW`` empty. NEVER set this in
    production; ``production_mode=True`` ignores it entirely."""

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

    # ── Plan 8 D8.1 — EventBus backend selector ────────────────────────
    event_bus_backend: Literal["inmemory", "redis"] = "inmemory"
    """Which :class:`EventBus` implementation the lifespan wires up.

      * ``inmemory`` (default) — the existing single-process bus; zero
        external deps. Suitable for single-worker deployments (which is
        the supported default tier per Plan 5).
      * ``redis`` — Plan 8 ``RedisStreamEventBus`` (XADD + consumer
        groups) so every uvicorn worker sees the same stream. Requires
        :attr:`redis_url` to be set and the optional ``[redis]`` extra
        installed (``uv sync --extra redis``).
    """

    redis_url: str | None = None
    """Connection URL for the optional Redis tier (D8.1 EventBus +
    D8.2 rate limiter). Required when either
    :attr:`event_bus_backend` or :attr:`rate_limit_backend` is set to
    ``redis``. Example: ``redis://localhost:6379/0``."""

    # ── Plan 8 D8.2 — RateLimit backend selector ───────────────────────
    rate_limit_backend: Literal["inmemory", "redis"] = "inmemory"
    """Which :class:`TokenBucketRateLimiter` backend
    :class:`RateLimitMiddleware` instantiates.

      * ``inmemory`` (default) — Plan 7 D7.7 in-process bucket table
        with LRU + sweeper. Per-worker buckets; safe for single-worker.
      * ``redis`` — Plan 8 D8.2 Lua-backed distributed bucket; shared
        across workers. Honours :attr:`redis_url` and (per
        :attr:`strict_backend`) either aborts or falls back to inmemory
        on Redis unavailability at boot.
    """

    # ── Plan 8 v2.1 MAJOR 4 — strict backend mode ──────────────────────
    strict_backend: bool = False
    """When ``True`` and a non-``inmemory`` backend (Redis) is
    unreachable at lifespan startup, the boot aborts with
    :class:`RuntimeError`. When ``False`` (default) the lifespan logs a
    ``warning`` and falls back to the ``inmemory`` implementation so a
    transient Redis outage doesn't take down a deployment that is
    otherwise happy to run single-worker. Set ``True`` in
    multi-worker production where the inmemory fallback would silently
    diverge per-worker state."""

    # ── Plan 9 D9.11 — multi-worker deployment safety check ───────────
    deployment_mode: Literal["single_worker", "multi_worker"] = "single_worker"
    """Declares whether this gg-relay instance is part of a
    multi-worker cluster (replicas > 1 sharing a Postgres + Redis).

    In ``multi_worker`` mode the lifespan validates that backend
    configuration is cluster-safe (see
    :data:`MULTI_WORKER_SAFE_BACKENDS`). The check is **always
    fail-fast**: any violation raises :class:`DeploymentModeError`
    and the lifespan aborts — preventing operators from shipping
    silently-broken configs where worker A's events never reach
    worker B's SSE clients.

    Single-worker mode (default) skips the check entirely.
    """

    # ── Plan 8 D8.10 — Postgres pool tuning ────────────────────────────
    # Task 2 reads these in ``store/engine.make_async_engine``. Defaults
    # match Plan 5 single-worker sqlite/Postgres profile; multi-worker
    # operators must keep ``workers × (db_pool_size + db_max_overflow)
    # ≤ postgres_max_connections`` (see docs/team-deployment.md).
    db_pool_size: int = 10
    """Base SQLAlchemy connection pool size per worker."""

    db_max_overflow: int = 5
    """Extra connections SQLAlchemy may open above ``db_pool_size``
    when the base pool is saturated, before queueing waiters."""

    db_pool_pre_ping: bool = True
    """Issue a cheap ``SELECT 1`` round-trip when checking a pooled
    connection out of the pool; catches dropped TCP sessions before
    SQLAlchemy hands them to the caller."""

    db_pool_recycle: int = 3600
    """Seconds after which an idle pooled connection is force-recycled
    (defends against backend-side ``idle_in_transaction_session_timeout``
    and load-balancer TCP keepalives). Default 1 hour."""

    db_slow_query_log_ms: int = 500
    """Slow-query threshold in milliseconds. Queries exceeding this are
    emitted at ``INFO`` via the SQLAlchemy event listener Task 2 wires
    in ``store/engine.py``. Set ``<= 0`` to disable the listener."""

    # ── Plan 8 D8.22 — role mapping (api_key_label → role) ─────────────
    role_mapping_raw: str = ""
    """``RELAY_ROLE_MAPPING_RAW`` env source for :attr:`role_mapping`.

    Format: ``"alice=admin,bob=submitter,carol=viewer"``. Stored as a
    string for the same reason as :attr:`api_keys_raw` — pydantic-settings
    would JSON-decode a dict value, but the dashboard-friendly format
    is intentionally simpler than JSON.
    """

    @property
    def role_mapping(self) -> dict[str, str]:
        """D8.22 — parsed view of :attr:`role_mapping_raw` as
        ``{api_key_label: role}`` where ``role ∈
        {"viewer","submitter","admin"}``. Empty dict (default) means
        every key falls through to ``"viewer"`` (least-privileged).
        """
        return _parse_role_mapping(self.role_mapping_raw)

    role_override_mode: Literal["db", "config"] = "db"
    """Plan 8 v2.3 BLOCKER 2 — source-of-truth for role lookups.

      * ``"db"`` (default) — dashboard ``/admin/keys`` page can edit
        roles at runtime via the ``api_keys`` table column; config
        :attr:`role_mapping` is only the boot seed.
      * ``"config"`` — :attr:`role_mapping` always wins; the dashboard
        edit endpoint is read-only. Use ONLY for emergency lockdown
        where operators want a one-pass restart-driven recovery path.
    """

    # ── Plan 8 D8.26 — dashboard cookie users ──────────────────────────
    dashboard_users_raw: str = ""
    """``RELAY_DASHBOARD_USERS_RAW`` env source for
    :attr:`dashboard_users`. Format:
    ``"alice=$2b$12$<bcrypt>,bob=$2b$12$<bcrypt>"``.

    Use ``python -c 'import bcrypt; print(bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode())'``
    to mint a hash. Bcrypt's char class is ``[./A-Za-z0-9$]`` — no
    commas, so the CSV split is unambiguous.
    """

    @property
    def dashboard_users(self) -> dict[str, str]:
        """D8.26 — parsed view of :attr:`dashboard_users_raw` as
        ``{username: bcrypt_hash}``. Empty dict (default) disables
        cookie auth entirely; the dashboard still works via the
        existing :attr:`dashboard_admin_password` flow. Task 3 reads
        this at lifespan boot to mint an internal API key per user.
        """
        return _parse_dashboard_users(self.dashboard_users_raw)

    # ── Plan 8 D8.28 — admin bootstrap ─────────────────────────────────
    admin_label: str = "admin"
    """Expected ``api_key_label`` of the bootstrap admin. Plan 8 Task 3
    seeds the role mapping so this label resolves to ``admin`` even
    when :attr:`role_mapping_raw` is empty, so a fresh deploy always
    has at least one operator capable of editing roles via
    ``/admin/keys``."""

    # ── Plan 8 D8.7 — alert rules + Feishu user mapping ────────────────
    alert_rules_json: str = ""
    """``RELAY_ALERT_RULES_JSON`` env source for :attr:`alert_rules`.

    JSON object, e.g.
    ``{"fail":["always"],"cancel":["timeout_recovered"],"complete":["tag=notify"]}``.
    Empty / missing falls back to :class:`AlertRouter`'s built-in
    defaults (fail always, cancel-timeout always, complete only when
    the session carries ``tag=notify``).
    """

    @property
    def alert_rules(self) -> dict[str, list[str]]:
        """D8.7 — parsed view of :attr:`alert_rules_json` as
        ``{rule_name: [conditions]}``. Used by ``AlertRouter`` to
        decide which terminal events fan out to IM backends. Empty
        dict (default) = use the in-router defaults.
        """
        return _parse_alert_rules_json(self.alert_rules_json)

    feishu_user_mapping_raw: str = ""
    """``RELAY_FEISHU_USER_MAPPING_RAW`` env source for
    :attr:`feishu_user_mapping`. Format:
    ``"alice=ou_xxx,bob=ou_yyy"`` mapping ``api_key_label`` →
    Feishu ``open_id`` so alert cards can ``@mention`` the session
    owner directly."""

    @property
    def feishu_user_mapping(self) -> dict[str, str]:
        """D8.7 — parsed view of :attr:`feishu_user_mapping_raw` as
        ``{api_key_label: open_id}``. Empty dict (default) means
        alert cards fall back to a non-@-mention shape (still useful
        for the team channel — just no notification ping).
        """
        return _parse_feishu_user_mapping(self.feishu_user_mapping_raw)

    # ── OTel ────────────────────────────────────────────────────────────
    # Plan 7 Task 15 / D7.23: ``otel_endpoint`` accepts both
    # ``RELAY_OTEL_ENDPOINT`` (canonical, ``RELAY_``-prefixed) and the
    # upstream OTel convention ``OTEL_EXPORTER_OTLP_ENDPOINT``. When both
    # are set ``RELAY_OTEL_ENDPOINT`` wins (first entry in AliasChoices)
    # so operators migrating from an OTel-native setup don't get a
    # surprise switch the moment they add the relay's own env file.
    otel_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "RELAY_OTEL_ENDPOINT",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
        ),
    )
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

    # ── Plan 7 Task 11 (D7.14) — secrets fail-fast helpers ─────────────
    def _feishu_configured(self) -> bool:
        """Return True if any Feishu credential is set.

        Treats "any one of these is set" as evidence that the operator
        *intended* to wire Feishu — so the consistency check then
        demands the full triad (app id, app secret, webhook secret).
        Avoids silent half-configured deployments where, say,
        ``feishu_app_id`` is set but ``feishu_webhook_secret`` is
        missing (inbound callbacks would 401 every time).
        """
        return any(
            getattr(self, fld, None)
            for fld in (
                "feishu_app_id",
                "feishu_app_secret",
                "feishu_webhook_secret",
            )
        )

    def validate_required_secrets(self) -> None:
        """Fail-fast secret validation called at lifespan startup.

        Dev mode (``production_mode=False``): warn on missing keys but
        continue; honours :attr:`allow_no_keys` to silence the warning
        entirely (for unit tests).

        Production mode (``production_mode=True``): raise
        :class:`RuntimeError` listing **every** problem in a single
        message so the operator can fix the env file in one pass
        rather than playing whack-a-mole. Three classes of problems
        are reported:

          1. ``RELAY_API_KEYS_RAW`` empty.
          2. Feishu partially configured (any credential set implies
             the full triad of app id / app secret / webhook secret).
          3. Database URL still equal to the sqlite dev default
             :data:`DEFAULT_SQLITE_URL` (production deployments MUST
             point at Postgres/MySQL — sqlite single-file storage
             is not safe for the durable event tier).
        """
        if not self.production_mode:
            if not self.api_keys_raw and not self.allow_no_keys:
                logger.warning(
                    "dev mode: no API keys configured "
                    "(RELAY_API_KEYS_RAW empty)"
                )
            return
        # Plan 8 D8.22 — empty role_mapping is legal (every key falls
        # back to ``viewer``) but operators should know that a
        # production deploy with no fine-grained roles will refuse
        # every mutation. Warn loudly so the silent-403 mystery doesn't
        # eat anyone's afternoon.
        if not self.role_mapping_raw:
            logger.warning(
                "production mode: RELAY_ROLE_MAPPING_RAW is empty; "
                "all API keys default to role='viewer' and will be "
                "denied any POST/PATCH/DELETE on /api/v1/sessions"
            )
        problems: list[str] = []
        if not self.api_keys_raw:
            problems.append("RELAY_API_KEYS_RAW required in production")
        if self._feishu_configured():
            for fld in (
                "feishu_app_id",
                "feishu_app_secret",
                "feishu_webhook_secret",
            ):
                if not getattr(self, fld, None):
                    problems.append(
                        f"{fld} required when Feishu is configured"
                    )
        if self.database_url == DEFAULT_SQLITE_URL:
            problems.append(
                "Postgres/MySQL URL required in production "
                "(RELAY_DATABASE_URL)"
            )
        if problems:
            raise RuntimeError(
                "missing required secrets: " + "; ".join(problems)
            )


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
