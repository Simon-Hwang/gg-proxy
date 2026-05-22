"""E2E test: real gg-plugins/install.sh through InstallShellAssembler.

Plan 2 Task 8 — needs ``GG_PLUGINS_HOME`` (or the default
``/data/workspace/github/gg-plugins``) to point at a checked-out
``gg-plugins`` repo with ``install.sh`` present. Marked
``requires_sdk`` because the installer execs ``node`` / ``npm``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from gg_relay.session.plugins import InstallShellAssembler
from gg_relay.session.spec import PluginManifest, SessionSpec

pytestmark = pytest.mark.requires_sdk


def _resolve_plugins_home() -> Path | None:
    explicit = os.environ.get("GG_PLUGINS_HOME")
    if explicit:
        p = Path(explicit)
        return p if (p / "install.sh").exists() else None
    default = Path("/data/workspace/github/gg-plugins")
    return default if (default / "install.sh").exists() else None


async def test_install_minimal_profile_e2e(tmp_path: Path) -> None:
    """End-to-end: profile=minimal → install.sh runs → InstallReport parsed."""
    plugins_home = _resolve_plugins_home()
    if plugins_home is None:
        pytest.skip(
            "gg-plugins/install.sh not found at $GG_PLUGINS_HOME or "
            "default /data/workspace/github/gg-plugins"
        )

    assembler = InstallShellAssembler(plugins_home=plugins_home)
    spec = SessionSpec(
        prompt="x",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
    )
    install_dir = tmp_path / "home"
    report = await assembler.prepare(spec, install_dir=install_dir)

    assert report.schema_version == "gg.install.v1"
    assert report.profile_id == "minimal"
    # rules-core is part of the minimal profile per the gg-plugins manifest.
    assert "rules-core" in report.selected_modules, (
        f"expected rules-core in selected_modules, got {report.selected_modules}"
    )
    assert (install_dir / ".claude" / "gg" / "install-state.json").exists()
    assert report.duration_ms > 0
