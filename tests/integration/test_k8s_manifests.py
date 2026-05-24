"""Plan 9 D9.4 — K8s manifest + Helm chart lint gates.

Skipped automatically when `helm` or `kubectl` are not on PATH;
that's the right behaviour for hermetic CI nodes that only have
the Python toolchain. When the binaries are present (CI nodes
that install them via setup-helm / setup-kubectl), these tests
enforce that:

1. ``helm lint deploy/helm/gg-relay`` exits 0.
2. ``helm template`` renders the chart with default values.
3. ``kubectl kustomize deploy/k8s/`` produces a non-empty YAML
   stream containing every expected ``kind:``.
4. ``kubectl apply --dry-run=client`` accepts the rendered
   kustomize stream.

The integration tests live alongside the Redis cross-worker
suite so the K8s lint gate runs in the same CI stage that
already pulls in the heavyweight tooling.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
K8S_DIR = ROOT / "deploy" / "k8s"
HELM_DIR = ROOT / "deploy" / "helm" / "gg-relay"

REQUIRED_KINDS = {
    "Namespace",
    "ServiceAccount",
    "ConfigMap",
    "Service",
    "Deployment",
    "PodDisruptionBudget",
    "HorizontalPodAutoscaler",
    "ServiceMonitor",
    "NetworkPolicy",
}

HELM_REQUIRED_KINDS = REQUIRED_KINDS - {"Namespace"} | {"Secret"}


def _has(binary: str) -> bool:
    return shutil.which(binary) is not None


@pytest.mark.skipif(not _has("helm"), reason="helm CLI not available")
def test_helm_lint_passes() -> None:
    """helm lint must exit 0; an [ERROR] in the body is a hard fail."""
    result = subprocess.run(  # noqa: S603 — fixed binary, no shell
        ["helm", "lint", str(HELM_DIR)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"helm lint exit={result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "[ERROR]" not in result.stdout
    assert "0 chart(s) failed" in result.stdout


@pytest.mark.skipif(not _has("helm"), reason="helm CLI not available")
def test_helm_template_renders_all_expected_kinds() -> None:
    """Default values render every required Kubernetes kind."""
    result = subprocess.run(  # noqa: S603 — fixed binary, no shell
        [
            "helm",
            "template",
            "gg-relay",
            str(HELM_DIR),
            "--namespace",
            "gg",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    kinds_seen = {
        line.split(":", 1)[1].strip()
        for line in result.stdout.splitlines()
        if line.startswith("kind:")
    }
    missing = HELM_REQUIRED_KINDS - kinds_seen
    assert not missing, f"helm template missing kinds: {missing}"


@pytest.mark.skipif(not _has("kubectl"), reason="kubectl CLI not available")
def test_kustomize_renders_all_expected_kinds() -> None:
    """deploy/k8s/ (kustomize entry) renders every expected kind."""
    result = subprocess.run(  # noqa: S603 — fixed binary, no shell
        ["kubectl", "kustomize", str(K8S_DIR)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    kinds_seen = {
        line.split(":", 1)[1].strip()
        for line in result.stdout.splitlines()
        if line.startswith("kind:")
    }
    missing = REQUIRED_KINDS - kinds_seen
    assert not missing, f"kustomize missing kinds: {missing}"


@pytest.mark.skipif(not _has("kubectl"), reason="kubectl CLI not available")
def test_kubectl_dry_run_accepts_kustomize_output() -> None:
    """The rendered manifests pass kubectl client-side validation."""
    render = subprocess.run(  # noqa: S603 — fixed binary, no shell
        ["kubectl", "kustomize", str(K8S_DIR)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert render.returncode == 0, render.stderr
    apply = subprocess.run(  # noqa: S603 — fixed binary, no shell
        ["kubectl", "apply", "--dry-run=client", "-f", "-"],
        input=render.stdout,
        capture_output=True,
        text=True,
        check=False,
    )
    assert apply.returncode == 0, (
        f"kubectl dry-run exit={apply.returncode}\n"
        f"stdout:\n{apply.stdout}\n"
        f"stderr:\n{apply.stderr}"
    )
