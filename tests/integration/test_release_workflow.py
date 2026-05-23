"""Static checks for ``.github/workflows/release.yml`` (Plan 7 Task 3).

These tests do not run the workflow — they assert structural invariants so
that accidental drift (a missing fork guard, a renamed step, a deleted
script reference) is caught at PR time rather than at tag-push time when
the only recourse is yanking a tag.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_RELEASE_YML = _REPO_ROOT / ".github" / "workflows" / "release.yml"


@pytest.fixture(scope="module")
def workflow_text() -> str:
    assert _RELEASE_YML.exists(), f"release workflow missing at {_RELEASE_YML}"
    return _RELEASE_YML.read_text(encoding="utf-8")


def test_release_yml_parses_as_yaml(workflow_text: str) -> None:
    """The workflow must be syntactically valid YAML (yamllint-equivalent).

    Skipped when ``PyYAML`` is not installed in the test env — the
    fork-guard and required-steps tests below still run on a plain string
    so the CI signal does not regress.
    """
    yaml = pytest.importorskip("yaml")
    parsed = yaml.safe_load(workflow_text)
    assert isinstance(parsed, dict)
    # PyYAML resolves unquoted ``on:`` to the Python literal ``True`` per
    # the YAML 1.1 boolean spec; accept either spelling.
    assert ("on" in parsed) or (True in parsed)
    assert "jobs" in parsed and "release" in parsed["jobs"]


def test_release_yml_fork_guard(workflow_text: str) -> None:
    """The release job must be gated by the canonical repository name.

    Without this guard, anyone who forks the repo and pushes a ``v*`` tag
    would trigger a publish to *their* GHCR with our action — which fails
    noisily and burns minutes. The exact equality check is mandatory.
    """
    pattern = re.compile(
        r"if:\s*github\.repository\s*==\s*['\"]gg-relay/gg-relay['\"]"
    )
    assert pattern.search(workflow_text), (
        "release.yml is missing the fork guard "
        "`if: github.repository == 'gg-relay/gg-relay'`"
    )


def test_release_yml_has_required_steps(workflow_text: str) -> None:
    """All four critical steps must be wired (version / license / docker / gh-release)."""
    required_fragments = (
        # 3-source version check (depends on Task 1 script).
        "scripts/check_version_sync.py",
        # pip-licenses gate (D7.1).
        "scripts/check_licenses.py",
        # Docker build & push to GHCR.
        "docker/build-push-action@v5",
        # GitHub release with auto-generated notes.
        "softprops/action-gh-release",
    )
    missing = [frag for frag in required_fragments if frag not in workflow_text]
    assert not missing, f"release.yml missing required step references: {missing}"
