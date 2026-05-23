#!/usr/bin/env python3
"""Three-way version sync check.

Compares:
  1. ``[project].version`` in ``pyproject.toml`` (the single source of
     truth that hatchling ships in the wheel metadata).
  2. ``gg_relay.__version__`` (which resolves via
     ``importlib.metadata.version("gg-relay")``).
  3. An optional expected version passed as the first CLI argument
     (used by ``release.yml`` to validate the tag matches what ships).

Exit code 0 when all values agree; exit code 1 with a clear message on
any mismatch.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_pyproject_version(pyproject_path: Path) -> str:
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)
    try:
        return str(data["project"]["version"])
    except KeyError as exc:
        raise SystemExit(
            f"check_version_sync: missing [project].version in {pyproject_path}"
        ) from exc


def _read_importlib_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("gg-relay")
    except PackageNotFoundError as exc:
        raise SystemExit(
            "check_version_sync: gg-relay is not installed; "
            "run `uv sync` (or `pip install -e .`) before invoking this script."
        ) from exc


def main(argv: list[str]) -> int:
    expected: str | None = argv[1] if len(argv) > 1 else None

    pyproject_version = _read_pyproject_version(_REPO_ROOT / "pyproject.toml")
    importlib_version = _read_importlib_version()

    mismatches: list[str] = []
    if pyproject_version != importlib_version:
        mismatches.append(
            f"pyproject.toml [project].version ({pyproject_version!r}) != "
            f"importlib.metadata.version('gg-relay') ({importlib_version!r})"
        )
    if expected is not None and expected != pyproject_version:
        mismatches.append(
            f"expected version from CLI arg ({expected!r}) != "
            f"pyproject.toml [project].version ({pyproject_version!r})"
        )
    if expected is not None and expected != importlib_version:
        mismatches.append(
            f"expected version from CLI arg ({expected!r}) != "
            f"importlib.metadata.version('gg-relay') ({importlib_version!r})"
        )

    if mismatches:
        sys.stderr.write("check_version_sync: version mismatch detected\n")
        for line in mismatches:
            sys.stderr.write(f"  - {line}\n")
        return 1

    parts = [f"pyproject={pyproject_version}", f"importlib={importlib_version}"]
    if expected is not None:
        parts.append(f"cli={expected}")
    print("check_version_sync: OK (" + ", ".join(parts) + ")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
