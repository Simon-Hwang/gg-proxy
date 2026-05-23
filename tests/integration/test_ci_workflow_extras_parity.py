"""CI workflow extras parity (Plan 7 Task 2 / D7.2).

Locks in the contract that every ``uv sync`` install step in
``.github/workflows/ci.yml`` carries the dev extras and that at least one
job pulls in the ``postgres`` extra so the SQLAlchemy + asyncpg surface is
exercised on CI. Also guards against accidental re-introduction of a
``loadtest`` extra (deliberately excluded from CI per Plan 7 v2.3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _iter_install_steps(workflow: dict[str, Any]) -> list[tuple[str, str]]:
    """Yield (job_name, run_block) for every step that invokes ``uv sync``."""
    steps: list[tuple[str, str]] = []
    for job_name, job in workflow.get("jobs", {}).items():
        for step in job.get("steps", []):
            run = step.get("run") if isinstance(step, dict) else None
            if isinstance(run, str) and "uv sync" in run:
                steps.append((job_name, run))
    return steps


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    assert CI_WORKFLOW.exists(), f"missing {CI_WORKFLOW}"
    parsed = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    return cast(dict[str, Any], parsed)


def test_workflow_has_uv_sync_install_step(workflow: dict[str, Any]) -> None:
    steps = _iter_install_steps(workflow)
    assert steps, "expected at least one `uv sync` install step in ci.yml"


def test_every_install_step_has_dev_extra(workflow: dict[str, Any]) -> None:
    steps = _iter_install_steps(workflow)
    for job_name, run in steps:
        assert "--extra dev" in run, (
            f"job {job_name!r}: `uv sync` step must include `--extra dev` (got: {run!r})"
        )


def test_at_least_one_job_has_postgres_extra(workflow: dict[str, Any]) -> None:
    steps = _iter_install_steps(workflow)
    assert any("--extra postgres" in run for _, run in steps), (
        "at least one CI job must install `--extra postgres` so the "
        "asyncpg/SQLAlchemy surface is exercised"
    )


def test_no_step_installs_loadtest_extra(workflow: dict[str, Any]) -> None:
    steps = _iter_install_steps(workflow)
    offenders = [job for job, run in steps if "--extra loadtest" in run]
    assert not offenders, f"loadtest extra must not be installed in CI; found in jobs: {offenders}"


def test_uses_frozen_lockfile(workflow: dict[str, Any]) -> None:
    steps = _iter_install_steps(workflow)
    for job_name, run in steps:
        assert "--frozen" in run, (
            f"job {job_name!r}: `uv sync` step must use `--frozen` to enforce "
            f"uv.lock parity (got: {run!r})"
        )


def test_setup_uv_action_with_cache(workflow: dict[str, Any]) -> None:
    """Each job that runs ``uv sync`` must first set up uv with cache enabled."""
    for job_name, job in workflow.get("jobs", {}).items():
        steps = job.get("steps", [])
        has_uv_sync = any(
            isinstance(s, dict) and isinstance(s.get("run"), str) and "uv sync" in s["run"]
            for s in steps
        )
        if not has_uv_sync:
            continue
        setup_uv = [
            s
            for s in steps
            if isinstance(s, dict)
            and isinstance(s.get("uses"), str)
            and s["uses"].startswith("astral-sh/setup-uv@")
        ]
        assert setup_uv, (
            f"job {job_name!r} runs `uv sync` but does not set up uv via astral-sh/setup-uv"
        )
        with_block = setup_uv[0].get("with", {}) or {}
        assert with_block.get("enable-cache") is True, (
            f"job {job_name!r}: setup-uv must set `enable-cache: true` (got: {with_block!r})"
        )
        assert with_block.get("cache-dependency-glob") == "uv.lock", (
            f"job {job_name!r}: setup-uv must pin cache to `uv.lock` (got: {with_block!r})"
        )
