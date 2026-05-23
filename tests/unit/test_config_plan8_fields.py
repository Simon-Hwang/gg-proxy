"""Plan 8 Phase 1 Task 1 — Dependency + Config baseline tests.

Guards the contract the rest of Plan 8 builds on:

  * ``pyproject.toml`` reinstates the ``[redis]`` extra (Plan 5 D5.15
    deletion reversed) so D8.1 / D8.2 multi-worker tier installs cleanly.
  * Default deps gain ``markdown-it-py`` + ``bleach`` for D8.5 comments
    rendering with XSS sanitization, plus ``cachetools`` for D8.29
    DBKeyResolver TTLCache (predeclared so later tasks don't churn
    pyproject again).
  * ``Config`` exposes the 15 new Plan 8 fields with single-worker-safe
    defaults (no Redis required, every backend = inmemory).
  * The CSV / JSON parsers reject malformed input loudly and never
    raise — operator typos must not refuse the whole boot.
"""
from __future__ import annotations

import logging
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _load_pyproject() -> dict:
    with PYPROJECT.open("rb") as f:
        return tomllib.load(f)


def test_pyproject_redis_extra_present() -> None:
    """D8.1+D8.2 reinstates the ``[redis]`` extra deleted in Plan 5 D5.15."""
    pyproj = _load_pyproject()
    extras = pyproj["project"]["optional-dependencies"]
    assert "redis" in extras, (
        "Plan 8 D8.1 requires the [redis] extra (Plan 5 D5.15 deletion "
        "is explicitly reversed by D8.1/D8.2)"
    )
    assert any("redis>=5.0" in dep for dep in extras["redis"]), (
        f"expected ``redis>=5.0`` pin in extras[redis], got {extras['redis']!r}"
    )
    # The ``all`` aggregate must opt into the new extra so
    # ``uv sync --extra all`` reproduces the multi-worker tier in CI.
    assert "redis" in extras["all"][0] or any(
        "redis" in dep for dep in extras["all"]
    ), "extras[all] must include redis after Plan 8 Task 1"


def test_pyproject_markdown_bleach_cachetools_default_deps() -> None:
    """D8.5 + D8.29 promote three new packages to *default* dependencies."""
    pyproj = _load_pyproject()
    deps = pyproj["project"]["dependencies"]
    assert any("markdown-it-py" in d for d in deps), (
        "D8.5 comments rendering requires markdown-it-py as a default dep"
    )
    assert any(d.startswith("bleach") for d in deps), (
        "D8.5 XSS sanitization requires bleach as a default dep"
    )
    assert any(d.startswith("cachetools") for d in deps), (
        "D8.29 DBKeyResolver TTLCache requires cachetools (predeclared "
        "at Task 1 to avoid a later pyproject churn)"
    )


def test_config_plan8_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """All 15 Plan 8 Config fields default to single-worker-friendly values."""
    # Run from an empty cwd so the repo's own .env doesn't leak in.
    monkeypatch.chdir(tmp_path)
    for env in (
        "RELAY_API_KEYS_RAW",
        "RELAY_EVENT_BUS_BACKEND",
        "RELAY_RATE_LIMIT_BACKEND",
        "RELAY_REDIS_URL",
        "RELAY_DB_POOL_SIZE",
        "RELAY_DB_MAX_OVERFLOW",
        "RELAY_DB_POOL_PRE_PING",
        "RELAY_DB_POOL_RECYCLE",
        "RELAY_DB_SLOW_QUERY_LOG_MS",
        "RELAY_ROLE_MAPPING_RAW",
        "RELAY_ROLE_OVERRIDE_MODE",
        "RELAY_DASHBOARD_USERS_RAW",
        "RELAY_ADMIN_LABEL",
        "RELAY_ALERT_RULES_JSON",
        "RELAY_FEISHU_USER_MAPPING_RAW",
        "RELAY_STRICT_BACKEND",
    ):
        monkeypatch.delenv(env, raising=False)

    from gg_relay.config import Config

    cfg = Config()  # type: ignore[call-arg]
    # D8.1 + D8.2 — single-worker default tier needs zero Redis.
    assert cfg.event_bus_backend == "inmemory"
    assert cfg.rate_limit_backend == "inmemory"
    assert cfg.redis_url is None
    # D8.10 — Postgres pool tuning defaults match the Plan 5 single-worker
    # baseline (10 + 5 = 15 conns/worker, comfortably under 50).
    assert cfg.db_pool_size == 10
    assert cfg.db_max_overflow == 5
    assert cfg.db_pool_pre_ping is True
    assert cfg.db_pool_recycle == 3600
    assert cfg.db_slow_query_log_ms == 500
    # D8.22 — empty role mapping = every key → "viewer" (least privilege).
    assert cfg.role_mapping == {}
    assert cfg.role_override_mode == "db"
    # D8.26 — empty dashboard users = cookie auth disabled (dev default).
    assert cfg.dashboard_users == {}
    # D8.28 — admin bootstrap label.
    assert cfg.admin_label == "admin"
    # D8.7 — empty rules + mapping fall back to AlertRouter built-ins.
    assert cfg.alert_rules == {}
    assert cfg.feishu_user_mapping == {}
    # v2.1 MAJOR 4 — strict_backend off so a Redis hiccup degrades
    # gracefully instead of hard-aborting the lifespan.
    assert cfg.strict_backend is False


class _RecordingHandler(logging.Handler):
    """Stdlib log handler that captures records into a list.

    We attach this directly to ``gg_relay.config.logger`` instead of
    relying on pytest's ``caplog`` fixture: when integration tests
    earlier in the run boot the FastAPI app, ``structlog.configure(...)``
    swaps the stdlib stream handler chain on the root logger, after
    which ``caplog``'s root-attached handler stops receiving records
    from named child loggers in the same process. Attaching to the
    target logger directly side-steps that and keeps the assertion
    deterministic regardless of test order.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_role_mapping_parser_warns_and_drops_invalid_role() -> None:
    """Unknown roles in RELAY_ROLE_MAPPING_RAW log a warning and are dropped."""
    from gg_relay.config import _parse_role_mapping
    from gg_relay.config import logger as config_logger

    handler = _RecordingHandler()
    previous_level = config_logger.level
    config_logger.setLevel(logging.WARNING)
    config_logger.addHandler(handler)
    try:
        result = _parse_role_mapping("alice=admin,bob=superuser,carol=submitter")
    finally:
        config_logger.removeHandler(handler)
        config_logger.setLevel(previous_level)

    # bob dropped (superuser ∉ {viewer, submitter, admin}); alice + carol kept.
    assert result == {"alice": "admin", "carol": "submitter"}
    messages = [rec.getMessage() for rec in handler.records]
    assert any(
        "invalid role" in msg.lower() and "bob" in msg for msg in messages
    ), f"expected warning for bob=superuser, got {messages!r}"


def test_role_mapping_parser_tolerates_blanks_and_missing_eq() -> None:
    """Empty tokens and tokens without ``=`` are silently skipped."""
    from gg_relay.config import _parse_role_mapping

    # blanks, no separator, only ``=`` — all dropped without raising.
    assert _parse_role_mapping("") == {}
    assert _parse_role_mapping(",,, ") == {}
    assert _parse_role_mapping("alice,bob=admin,") == {"bob": "admin"}


def test_dashboard_users_parser_handles_bcrypt_dollar_signs() -> None:
    """bcrypt hashes carry ``$`` — parser must split on first ``=`` only."""
    from gg_relay.config import _parse_dashboard_users

    bcrypt_alice = "$2b$12$abcdefghijklmnopqrstuv0123456789ABCDEFGHIJKLMNOPQRSTU"
    bcrypt_bob = "$2b$12$ZYXWVUTSRQPONMLKJIHGFE9876543210zyxwvutsrqponmlkjihgfe"
    raw = f"alice={bcrypt_alice},bob={bcrypt_bob}"
    result = _parse_dashboard_users(raw)

    assert result == {"alice": bcrypt_alice, "bob": bcrypt_bob}


def test_alert_rules_json_parser_falls_back_on_garbage() -> None:
    """Malformed JSON or wrong shape returns ``{}`` + warns; never raises."""
    from gg_relay.config import _parse_alert_rules_json

    # Valid happy path.
    parsed = _parse_alert_rules_json(
        '{"fail":["always"],"cancel":["timeout_recovered"]}'
    )
    assert parsed == {
        "fail": ["always"],
        "cancel": ["timeout_recovered"],
    }
    # Garbage = fall back to defaults (= {}); the warning is emitted to
    # the gg_relay.config logger but the contract under test is that
    # parsing never raises and returns ``{}``, not that any specific log
    # surface receives the record.
    assert _parse_alert_rules_json("not json {{{") == {}
    # JSON array (wrong shape) = fall back.
    assert _parse_alert_rules_json('["fail"]') == {}
    # Right outer shape but non-list value = drop the rule, keep the rest.
    parsed = _parse_alert_rules_json('{"fail":"always","cancel":["x"]}')
    assert parsed == {"cancel": ["x"]}


def test_feishu_user_mapping_parser_basic() -> None:
    """Standard ``alice=ou_xxx,bob=ou_yyy`` parses to the expected dict."""
    from gg_relay.config import _parse_feishu_user_mapping

    result = _parse_feishu_user_mapping("alice=ou_xxx,bob=ou_yyy")
    assert result == {"alice": "ou_xxx", "bob": "ou_yyy"}
    # Empty input = empty dict.
    assert _parse_feishu_user_mapping("") == {}
