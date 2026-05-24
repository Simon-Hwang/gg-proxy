"""Prometheus scrape config sanity — Plan 8 D8.13 / Task 20.

Asserts that ``deploy/prometheus/prometheus.yml`` carries a single
job named ``gg-relay`` pointed at the FastAPI ``/metrics`` endpoint
on the dev compose host. Also exercises the docker-compose dev
profile gate so a future rename of ``profiles: [observability]``
breaks loudly here instead of silently leaving the stack disabled.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROM_CONFIG = REPO_ROOT / "deploy" / "prometheus" / "prometheus.yml"
COMPOSE_DEV = REPO_ROOT / "deploy" / "docker-compose.dev.yml"


def test_prometheus_config_exists_and_targets_gg_relay() -> None:
    yaml = pytest.importorskip("yaml")
    assert PROM_CONFIG.is_file()
    data = yaml.safe_load(PROM_CONFIG.read_text(encoding="utf-8"))
    jobs = [j["job_name"] for j in data["scrape_configs"]]
    assert "gg-relay" in jobs
    job = next(j for j in data["scrape_configs"] if j["job_name"] == "gg-relay")
    targets = job["static_configs"][0]["targets"]
    assert any("gg-relay:8000" in t for t in targets)


def test_docker_compose_dev_has_observability_and_maintenance_profiles() -> None:
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(COMPOSE_DEV.read_text(encoding="utf-8"))
    services = data["services"]
    assert "prometheus" in services
    assert "grafana" in services
    assert "maintenance" in services
    assert "observability" in services["prometheus"]["profiles"]
    assert "observability" in services["grafana"]["profiles"]
    assert "maintenance" in services["maintenance"]["profiles"]
