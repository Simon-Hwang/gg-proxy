"""Process-wide configuration backed by pydantic-settings.

All env vars are prefixed with ``RELAY_``; secrets are typed as
:class:`SecretStr` so accidental logging shows ``**********`` instead of
the raw value.

The ``check-secrets`` CLI command validates that the required-for-production
fields are present and fails fast if not.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


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

    Stored as a string because pydantic-settings would otherwise try to
    JSON-decode a list value out of the env var (and fail on plain CSV).
    """

    @property
    def api_keys(self) -> list[SecretStr]:
        return [SecretStr(s) for s in _split_csv(self.api_keys_raw)]

    """Authorised API keys for X-API-Key header (comma-separated env)."""

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
    if isinstance(value, list):
        return not value
    if isinstance(value, SecretStr):
        return not value.get_secret_value()
    return False
