"""Plan 8 Task 21 — final gate tests for spec / changelog drift.

These tests are LIGHTWEIGHT artefact checks that guard the v0.8.0
release surface from accidental rollback:

  * ``test_spec_has_plan8_section`` — the spec carries the §17.8
    "Plan 8 — Team Collaboration & Cost Attribution" section with a
    spot-check of the major decisions referenced. The full decision
    table can be reflowed; the spot-checked Dx.y identifiers must not
    drop out.
  * ``test_changelog_has_0_8_0`` — ``CHANGELOG.md`` carries a
    ``[0.8.0] - 2026-05-24`` section with ``### Added`` and
    ``### Security`` subsections at minimum (Keep-a-Changelog shape).
  * ``test_version_is_0_8_0`` — pyproject + importlib +
    ``gg_relay.__version__`` all report ``0.8.0`` (single source of
    truth contract from Plan 7 D7.3).

If a future Plan 9+ release supersedes these literal version strings,
update this file alongside the same release commit that bumps
``pyproject.toml`` — the test failure is the reminder.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import version as _importlib_version
from pathlib import Path

import gg_relay

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_spec_has_plan8_section() -> None:
    """Spec carries §17.8 Plan 8 section with the spot-checked decisions."""

    spec_path = (
        _REPO_ROOT
        / "docs"
        / "superpowers"
        / "specs"
        / "2026-05-22-sdk-bootstrap-and-runtime-design.md"
    )
    text = spec_path.read_text(encoding="utf-8")

    # Section header — the exact §17.8 title locked at Task 21.
    assert "17.8 Plan 8 Team Collaboration" in text, (
        "spec §17.8 'Plan 8 Team Collaboration' header missing"
    )
    assert "v0.8.0" in text, "spec missing v0.8.0 release tag in §17.8"

    # Spot-check the major decisions referenced in the closing
    # decision table. Any of these dropping out signals a rollback.
    for did in [
        "D8.0",
        "D8.3",
        "D8.4",
        "D8.5",
        "D8.6",
        "D8.7",
        "D8.10",
        "D8.13",
        "D8.14",
        "D8.20",
        "D8.21",
        "D8.22",
        "D8.24",
        "D8.26",
        "D8.29",
        "D8.30",
    ]:
        assert did in text, f"spec missing Plan 8 decision {did}"

    # Boundary decisions (deferred items) must be acknowledged in
    # the closing summary so future readers know what was scoped
    # out vs forgotten.
    for token in ["deferred", "Phase 4", "HITL mute", "tsvector"]:
        assert token in text, f"spec missing boundary-decision marker {token!r} in §17.8"


def test_changelog_has_0_8_0() -> None:
    """CHANGELOG carries a [0.8.0] - 2026-05-24 section with key subsections."""

    chl = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "[0.8.0] - 2026-05-24" in chl, (
        "CHANGELOG.md missing '[0.8.0] - 2026-05-24' release section"
    )
    # Spot-check the Keep-a-Changelog subsections every Plan 8
    # release surface must publish.
    for subsection in [
        "### Added",
        "### Changed",
        "### Security",
        "### Migrations",
    ]:
        assert subsection in chl, f"CHANGELOG.md [0.8.0] missing {subsection!r} subsection"
    # Spot-check that the Alembic migration list is wired up.
    for rev in ["0006", "0007", "0008", "0009", "0010", "0011"]:
        assert rev in chl, f"CHANGELOG.md missing Alembic revision {rev!r}"


def test_version_is_0_8_0() -> None:
    """pyproject + importlib.metadata + gg_relay.__version__ all 0.8.0."""

    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)
    pyproject_ver = pyproject["project"]["version"]
    assert pyproject_ver == "0.8.0", f"pyproject.toml version != 0.8.0: {pyproject_ver!r}"

    importlib_ver = _importlib_version("gg-relay")
    assert importlib_ver == "0.8.0", (
        f"importlib.metadata.version('gg-relay') != 0.8.0: {importlib_ver!r}"
    )

    assert gg_relay.__version__ == "0.8.0", (
        f"gg_relay.__version__ != 0.8.0: {gg_relay.__version__!r}"
    )
