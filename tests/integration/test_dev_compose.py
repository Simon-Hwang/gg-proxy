"""Dev compose Jaeger wiring guards (Plan 7 D7.11).

These tests pin the four properties of ``deploy/docker-compose.dev.yml``
that the operator docs (``docs/tracing.md``) promise:

1. There is a ``jaeger`` service.
2. The UI port (16686) and the OTLP-gRPC ingest port (4317) are
   published to the host.
3. The relay's ``RELAY_OTEL_ENDPOINT`` env var points at the sibling
   ``jaeger`` service.
4. ``platform: linux/amd64`` is set on the jaeger service so arm64
   Macs (Jaeger all-in-one publishes amd64-only tags as of 1.57) do
   not silently fail the image pull.

We parse the YAML with :mod:`yaml` (already a transitive dep) and
avoid shelling out to ``docker compose config``; that command would
need Docker installed in CI just to validate a static file.
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "deploy" / "docker-compose.dev.yml"


def _load_compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def _env_dict(env: object) -> dict[str, str]:
    """Normalise both compose env shapes (mapping OR list of ``K=V``)."""
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    if isinstance(env, list):
        out: dict[str, str] = {}
        for entry in env:
            key, _, val = str(entry).partition("=")
            out[key] = val
        return out
    raise TypeError(f"unsupported environment shape: {type(env)!r}")


def test_dev_compose_includes_jaeger() -> None:
    compose = _load_compose()
    assert "jaeger" in compose["services"], (
        "deploy/docker-compose.dev.yml is missing the jaeger service "
        "(Plan 7 D7.11)"
    )


def test_dev_compose_jaeger_ports() -> None:
    compose = _load_compose()
    ports = [str(p) for p in compose["services"]["jaeger"]["ports"]]
    assert any(p.startswith("16686:16686") for p in ports), (
        f"jaeger UI port 16686 not published; got {ports!r}"
    )
    assert any(p.startswith("4317:4317") for p in ports), (
        f"jaeger OTLP-gRPC port 4317 not published; got {ports!r}"
    )


def test_dev_compose_relay_points_to_jaeger() -> None:
    compose = _load_compose()
    env = _env_dict(compose["services"]["gg-relay"]["environment"])
    assert env.get("RELAY_OTEL_ENDPOINT") == "http://jaeger:4317", (
        "gg-relay must export RELAY_OTEL_ENDPOINT=http://jaeger:4317 so "
        "OtelSubscriber auto-wires against the sibling jaeger service "
        f"(got {env.get('RELAY_OTEL_ENDPOINT')!r})"
    )


def test_dev_compose_jaeger_platform_amd64() -> None:
    """Pin ``platform: linux/amd64`` so arm64 Macs still pull a valid
    image (Jaeger all-in-one publishes amd64-only tags as of 1.57)."""
    compose = _load_compose()
    assert compose["services"]["jaeger"].get("platform") == "linux/amd64", (
        "jaeger service must set `platform: linux/amd64`; without it "
        "arm64 Macs hit a manifest-not-found error on `docker compose up`"
    )
