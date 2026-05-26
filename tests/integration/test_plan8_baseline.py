"""Plan 8 Phase 0 Task 0 — Plan 7 v0.7.0 baseline gate.

These tests are CONTRACT GATES: if Plan 7 v2.3 is not fully merged,
all subsequent Plan 8 work is invalid. Failure here BLOCKS Plan 8.

Gates checked:
  1. ``gg-relay`` reports version ``0.7.0`` from both ``importlib.metadata``
     and ``pyproject.toml``.
  2. ``docs/api-snapshot-v0.7.0.json`` exists and is a non-trivial OpenAPI
     document (Plan 8 frozen modification baseline).
  3. D7.26 collaboration metadata contract (``api_keys_with_labels`` parser
     + ``request.state.api_key_label`` middleware) is live in ``src/``.
  4. Alembic head is monotonically advancing through the planned Plan 8
     migrations (``0006``–``0011``). Phase 0 froze on ``0005``; the gate
     follows the work and tracks whichever revision the current task has
     just landed so a regression that drops a migration is caught
     immediately.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "gg_relay"


def _src_contains(symbol: str) -> bool:
    """Return True if ``symbol`` appears in at least one file under ``src/``."""
    for path in SRC_ROOT.rglob("*.py"):
        try:
            if symbol in path.read_text(encoding="utf-8"):
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


def test_v0_7_0_release_version() -> None:
    """gg-relay reports a Plan-7-or-newer release.

    Phase 0 froze on ``0.7.0`` as the immutable Plan 8 baseline. Plan 8
    Task 21 ships ``0.8.0`` as the release that contains the work this
    test once asserted as "still pending"; we now accept any version
    ``>= 0.7.0`` so this gate stays meaningful through Plan 9+ without
    needing a per-release version bump.
    """
    from importlib.metadata import version

    importlib_ver = version("gg-relay")
    assert importlib_ver >= "0.7.0", (
        f"gg-relay below Plan 7 baseline: {importlib_ver!r}"
    )

    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        pyproj = tomllib.load(f)
    pyproject_ver = pyproj["project"]["version"]
    assert pyproject_ver >= "0.7.0", (
        f"pyproject.toml below Plan 7 baseline: {pyproject_ver!r}"
    )
    # The two sources still have to agree — that is the whole point of
    # the Plan 7 D7.3 single-source-of-truth contract.
    assert importlib_ver == pyproject_ver, (
        f"version drift: importlib={importlib_ver!r} "
        f"pyproject={pyproject_ver!r}"
    )


def test_api_snapshot_v070_baseline_exists() -> None:
    """Plan 8 baseline freeze: docs/api-snapshot-v0.7.0.json present and
    non-empty (used as the immutable diff base while Plan 8 evolves the
    runtime API surface)."""
    p = REPO_ROOT / "docs" / "api-snapshot-v0.7.0.json"
    assert p.exists(), "Plan 8 Task 0 must freeze v0.7.0 OpenAPI baseline"
    data = json.loads(p.read_text())
    assert "openapi" in data, "baseline missing 'openapi' field"
    assert "paths" in data, "baseline missing 'paths' field"
    assert len(data["paths"]) > 5, "baseline has suspiciously few paths"


def test_d7_26_contract_landed() -> None:
    """Plan 7 D7.26 collaboration metadata (api_keys_with_labels +
    request.state.api_key_label) must be live in src/."""
    assert _src_contains("api_keys_with_labels"), (
        "D7.26 api_keys_with_labels parser not found in src/"
    )
    assert _src_contains("request.state.api_key_label"), (
        "D7.26 api_key_label request.state assignment not found in src/"
    )


def test_alembic_head_advances_with_plan_8() -> None:
    """Alembic head must monotonically advance through Plans 8, 9, v3.

    Phase 0 froze on ``0005`` (Plan 7 baseline). Each Plan 8 task that
    lands a migration bumps this gate so a regression that drops a
    migration is caught immediately. Plan 9 v0.9.0 (pre-prod
    simplification) collapses the original 0012a/0012b/0013 trio
    into a single 0012 (events.seq + dashboard_internal_keys).

    Plan v3 §B.1 adds ``0013`` (``user_credentials`` for per-user
    upstream credentials). Head must therefore be exactly ``0013``.
    """
    result = subprocess.run(
        ["uv", "run", "alembic", "heads"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert "0013" in result.stdout, (
        f"alembic head not 0013: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
