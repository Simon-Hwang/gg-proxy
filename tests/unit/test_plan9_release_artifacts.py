"""Plan 9 D9.0b — release.yml / Dockerfile / pyproject parity.

Reviewer G Round 3 BLOCKER caught that:

* ``release.yml`` did NOT ship ``--extra redis`` so the built Docker
  image lacked the redis-py client.
* ``Dockerfile.service`` did NOT install the ``[redis]`` extra so
  flipping ``RELAY_EVENT_BUS_BACKEND=redis`` in a v0.8.x container
  would silently CrashLoopBackOff at lifespan init.
* ``pyproject.toml`` ``[redis]`` extra had no upper bound on
  ``redis>=5.0``, leaving the door open for a transparent 6.x bump
  on the next ``pip install --upgrade``.

These tests guard the D9.0b fixes (shipped in v0.9.0) so future
CI changes can't regress the matrix.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_release_yml_installs_redis_extra() -> None:
    f = REPO_ROOT / ".github" / "workflows" / "release.yml"
    body = f.read_text()
    # Must explicitly enumerate ``--extra redis`` so the built image
    # has the redis-py client baked in.
    assert "--extra redis" in body, (
        "release.yml must install the [redis] extra for D9.0b — "
        "without it the published Docker image cannot use the Plan 9.1 "
        "RedisStreamEventBus / RedisRateLimitStore even after the "
        "operator sets RELAY_EVENT_BUS_BACKEND=redis."
    )


def test_dockerfile_service_installs_redis_extra() -> None:
    f = REPO_ROOT / "deploy" / "docker" / "Dockerfile.service"
    body = f.read_text()
    # The pip install line must include redis in the extras list.
    # Match any subset that includes 'redis'.
    assert "redis" in body.lower(), (
        "Dockerfile.service should install the [redis] extra (D9.0b)"
    )
    # Specifically — check that the pip install line names it.
    pip_install_lines = [
        line for line in body.splitlines() if "pip install" in line
    ]
    assert pip_install_lines, "Dockerfile must have a `pip install` step"
    assert any(
        "redis" in line for line in pip_install_lines
    ), (
        "the pip install step must list the [redis] extra "
        f"(found: {pip_install_lines})"
    )


def test_pyproject_redis_has_upper_bound() -> None:
    """D9.0b — lock ``redis<6.0`` to prevent silent major-version
    drift via ``pip install --upgrade``."""
    f = REPO_ROOT / "pyproject.toml"
    body = f.read_text()
    # Lines like ``redis = ["redis>=5.0,<6.0"]`` — both bounds.
    redis_lines = [
        line for line in body.splitlines() if line.strip().startswith("redis =")
    ]
    assert redis_lines, "pyproject.toml must declare a [redis] extra"
    assert any(
        "<6.0" in line for line in redis_lines
    ), f"redis extra must lock <6.0 (found: {redis_lines})"


def test_pyproject_dev_includes_fakeredis() -> None:
    """D9.0b — test-only fakeredis under [dev], NOT under [redis]
    (Reviewer H Round 4 MAJOR fix — don't pollute the production
    extra with a test helper)."""
    f = REPO_ROOT / "pyproject.toml"
    body = f.read_text()
    # fakeredis should appear once in [dev] (a Plan 9 addition).
    assert body.count("fakeredis") >= 1
    # It must NOT appear in the [redis] extra line.
    redis_lines = [
        line for line in body.splitlines() if line.strip().startswith("redis =")
    ]
    for line in redis_lines:
        assert "fakeredis" not in line, (
            "fakeredis must NOT live under the production [redis] extra — "
            "it's a test helper and belongs in [dev]"
        )


def test_pyproject_dev_includes_test_infra() -> None:
    """Plan 9 adds pytest-xdist + testcontainers for the multi-worker
    integration tests; they belong under [dev]."""
    f = REPO_ROOT / "pyproject.toml"
    body = f.read_text()
    for required in ("pytest-xdist", "testcontainers"):
        assert required in body, (
            f"pyproject.toml [dev] should include {required} for "
            "Plan 9.1 integration tests"
        )
