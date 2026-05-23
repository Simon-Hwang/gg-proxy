"""[loadtest] extra parity + load_test.py syntax check (Plan 7 Task 4 / D7.10).

Two guardrails for the Locust load-test scaffold:

1. ``test_loadtest_extra_defined`` — parses ``pyproject.toml`` and
   asserts the ``[loadtest]`` optional dependency exists and pins
   ``locust>=2.20``. Pure file read; no install required.

2. ``test_load_test_py_lint`` — verifies ``scripts/load_test.py`` is a
   syntactically valid Locust file. Modern Locust (2.x) has no
   ``--check`` flag; ``--list`` is the canonical "import + list user
   classes, exit 0 if OK" command that performs the same load-and-
   validate roundtrip. Skipped (with a clear reason) when the
   ``[loadtest]`` extra is not installed, so the core test matrix
   never has to pull in Locust just for this guard.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
LOAD_TEST = REPO_ROOT / "scripts" / "load_test.py"


def _loadtest_extra() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    extras = data.get("project", {}).get("optional-dependencies", {})
    return list(extras.get("loadtest") or [])


def test_loadtest_extra_defined() -> None:
    """pyproject.toml must declare a ``loadtest`` extra pinning locust>=2.20."""
    pins = _loadtest_extra()
    assert pins, "pyproject.toml [project.optional-dependencies] must define `loadtest`"

    locust_pin = next((p for p in pins if p.lower().startswith("locust")), None)
    assert locust_pin is not None, (
        f"[loadtest] extra must include a `locust` requirement (got: {pins!r})"
    )
    assert ">=2.20" in locust_pin.replace(" ", ""), (
        f"[loadtest] extra must pin `locust>=2.20` (got: {locust_pin!r})"
    )


def test_load_test_py_lint() -> None:
    """scripts/load_test.py must load cleanly under the real Locust runner."""
    assert LOAD_TEST.exists(), f"missing {LOAD_TEST}"

    locust_bin = shutil.which("locust")
    if locust_bin is None:
        try:
            import locust  # noqa: F401
        except ImportError:
            pytest.skip(
                "locust not installed — install via `pip install -e '.[loadtest]'` "
                "or `uv sync --extra loadtest` to enable this lint."
            )
        locust_bin = sys.executable
        cmd = [locust_bin, "-m", "locust", "-f", str(LOAD_TEST), "--list"]
    else:
        cmd = [locust_bin, "-f", str(LOAD_TEST), "--list"]

    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"locust failed to load scripts/load_test.py (exit {result.returncode}).\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    stdout = result.stdout
    for user_cls in ("RESTUser", "DashboardUser", "SSEUser"):
        assert user_cls in stdout, (
            f"locust --list did not expose {user_cls}; "
            f"got stdout: {stdout!r}"
        )
