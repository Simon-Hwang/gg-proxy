"""Guard that ``.env.example`` stays in sync with :class:`Config`.

Every public env-backed field on ``Config`` MUST appear as a comment or
key in ``.env.example`` so operators always have a discoverable
template. Failing this test means a field was added without updating the
template (Plan 5 Task 7).
"""
from __future__ import annotations

from pathlib import Path

from gg_relay.config import Config

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _expected_env_vars() -> set[str]:
    """All RELAY_* env var names a user could set, derived from Config."""
    out: set[str] = set()
    for name in Config.model_fields:
        out.add(f"RELAY_{name.upper()}")
    return out


def test_env_example_exists():
    assert ENV_EXAMPLE.is_file(), ".env.example must live at repo root"


def test_env_example_mentions_every_config_field():
    body = ENV_EXAMPLE.read_text(encoding="utf-8")
    missing = sorted(name for name in _expected_env_vars() if name not in body)
    assert not missing, f"missing from .env.example: {missing}"
