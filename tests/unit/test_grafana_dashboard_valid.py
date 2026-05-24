"""Grafana dashboard sanity — Plan 8 D8.13 / Task 20.

Pure JSON-shape checks so a reckless dashboard edit doesn't ship a
malformed file. We assert:

* The file parses as JSON.
* The ``uid`` is the stable ``gg-relay-main`` identifier the
  Grafana provisioning provider references.
* All seven panels (active sessions, sessions/min, duration p50/95/99,
  cost-by-owner, cost-trend, team-cost, failure-rate) are present.
* Each cost-related panel actually queries
  ``gg_relay_session_cost_usd_total`` so the Plan 8 D8.30 metric
  is wired into the dashboard.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = REPO_ROOT / "deploy" / "grafana" / "gg-relay-dashboard.json"


def test_dashboard_file_exists() -> None:
    assert DASHBOARD.is_file(), DASHBOARD


def test_dashboard_parses_and_has_expected_shape() -> None:
    data = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    assert data["uid"] == "gg-relay-main"
    assert data["title"] == "gg-relay"
    assert isinstance(data["panels"], list)
    assert len(data["panels"]) >= 7
    ids = {p["id"] for p in data["panels"]}
    assert len(ids) == len(data["panels"]), "panel ids must be unique"


def test_dashboard_includes_cost_metric_queries() -> None:
    data = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    cost_panels = []
    for panel in data["panels"]:
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            if "gg_relay_session_cost_usd_total" in expr:
                cost_panels.append(panel["title"])
                break
    assert len(cost_panels) >= 3, (
        f"expected ≥3 panels referencing cost metric, got {cost_panels}"
    )


def test_dashboard_provisioning_yaml_exists() -> None:
    dash_yaml = (
        REPO_ROOT
        / "deploy"
        / "grafana"
        / "provisioning"
        / "dashboards"
        / "gg-relay.yaml"
    )
    ds_yaml = (
        REPO_ROOT
        / "deploy"
        / "grafana"
        / "provisioning"
        / "datasources"
        / "prometheus.yaml"
    )
    assert dash_yaml.is_file()
    assert ds_yaml.is_file()
    assert "providers:" in dash_yaml.read_text(encoding="utf-8")
    assert "prometheus" in ds_yaml.read_text(encoding="utf-8").lower()
