"""Smoke checks for the deploy/ artifacts (Plan 5 Task 9).

These tests don't run docker — they just guard against accidental
deletion / rename of the deployment surface and verify the documented
security invariants (D5.6=A: prod compose MUST NOT mount the docker
socket; dev compose MUST).
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy"


def test_service_dockerfile_exists():
    df = DEPLOY_DIR / "docker" / "Dockerfile.service"
    assert df.is_file()
    body = df.read_text()
    assert "FROM python:3.12-slim" in body
    # Service image must NOT bundle Node / claude-cli / gg-plugins (D5.13).
    assert "@anthropic-ai/claude-code" not in body
    assert "GG_PLUGINS" not in body
    # Must pull the docker CLI from the official image.
    assert "FROM docker:24.0-cli" in body


def test_docker_compose_dev_mounts_socket():
    f = DEPLOY_DIR / "docker-compose.dev.yml"
    assert f.is_file()
    body = f.read_text()
    assert "/var/run/docker.sock:/var/run/docker.sock" in body, (
        "dev compose MUST bind-mount the docker socket so DockerExecutor "
        "can talk to the host daemon"
    )


def test_docker_compose_prod_does_not_mount_socket():
    f = DEPLOY_DIR / "docker-compose.prod.yml"
    assert f.is_file()
    body = f.read_text()
    # Strip comment lines before checking — the comment block intentionally
    # *mentions* /var/run/docker.sock as a thing we deliberately omit.
    code_lines = [
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert "docker.sock" not in code, (
        "prod compose MUST NOT bind-mount the docker socket — Plan 5 D5.6=A."
    )
    # Must reference the production-recommended per-session socket dir.
    assert "/var/run/gg-relay" in code


def test_docker_readme_describes_relationship():
    f = DEPLOY_DIR / "docker" / "README.md"
    assert f.is_file()
    body = f.read_text()
    assert "gg-relay-service" in body
    assert "gg-relay-runner" in body
