"""Tests for Plan 7 Task 1 — version single source of truth.

Asserts the three-way invariant:
  pyproject.toml [project].version
    == importlib.metadata.version("gg-relay")
    == gg_relay.__version__

And exercises ``scripts/check_version_sync.py`` exit codes for both
match and mismatch arguments.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from importlib.metadata import version as _importlib_version
from pathlib import Path

import gg_relay

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_SCRIPT = _REPO_ROOT / "scripts" / "check_version_sync.py"


def _pyproject_version() -> str:
    with _PYPROJECT.open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def test_pyproject_version_matches_importlib() -> None:
    """pyproject.toml is the single source of truth; importlib & gg_relay must echo it."""

    pyproject_value = _pyproject_version()
    importlib_value = _importlib_version("gg-relay")

    assert pyproject_value == importlib_value, (
        f"pyproject.toml ({pyproject_value!r}) and "
        f"importlib.metadata.version('gg-relay') ({importlib_value!r}) disagree"
    )
    assert gg_relay.__version__ == pyproject_value, (
        f"gg_relay.__version__ ({gg_relay.__version__!r}) does not match "
        f"pyproject.toml ({pyproject_value!r})"
    )


def test_check_version_sync_script_match() -> None:
    """Passing the current pyproject version as the expected arg exits 0."""

    current = _pyproject_version()
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), current],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "OK" in result.stdout


def test_check_version_sync_script_mismatch() -> None:
    """A bogus expected version exits 1 with a mismatch message on stderr."""

    bogus = "0.0.0-definitely-not-real"
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), bogus],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, (
        f"expected exit 1, got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "mismatch" in result.stderr.lower()
    assert bogus in result.stderr
