"""Tests for SessionSpec / PluginManifest / Decision."""
from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest

from gg_relay.session.spec import (
    Decision,
    PluginManifest,
    RuntimeHandle,
    SessionSpec,
)


class TestPluginManifest:
    def test_profile_only(self):
        m = PluginManifest(profile="minimal")
        assert m.to_install_argv() == ["--profile", "minimal", "--home", "/root", "--json"]

    def test_modules_only(self):
        m = PluginManifest(modules=("rules-python", "skills-security"))
        assert m.to_install_argv() == [
            "--modules", "rules-python,skills-security", "--home", "/root", "--json"
        ]

    def test_skills_with_overrides(self):
        m = PluginManifest(
            profile="python",
            skills=("brainstorming",),
            with_components=("lang:go",),
            without_components=("capability:learning",),
        )
        argv = m.to_install_argv(home_dir="/tmp/t")
        assert argv == [
            "--profile", "python",
            "--skills", "brainstorming",
            "--with", "lang:go",
            "--without", "capability:learning",
            "--home", "/tmp/t",
            "--json",
        ]

    def test_empty_manifest_rejected(self):
        with pytest.raises(ValueError, match="至少一个"):
            PluginManifest()


class TestSessionSpec:
    def test_minimal(self, tmp_path: Path):
        spec = SessionSpec(
            prompt="hello",
            cwd=tmp_path,
            plugins=PluginManifest(profile="minimal"),
        )
        assert spec.executor == "docker"  # 默认 docker
        assert spec.timeout_s == 1800

    def test_inprocess_override(self, tmp_path: Path):
        spec = SessionSpec(
            prompt="hello",
            cwd=tmp_path,
            plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )
        assert spec.executor == "inprocess"


class TestDecision:
    def test_string_values(self):
        assert Decision.ACCEPT == "accept"
        assert Decision.DENY == "deny"
        assert Decision.NEEDS_HITL == "needs_hitl"


class TestRuntimeHandle:
    def test_frozen(self):
        from dataclasses import FrozenInstanceError
        from datetime import datetime
        h = RuntimeHandle(
            backend="inprocess",
            runtime_id="task-1",
            transport=None,  # type: ignore[arg-type]
            started_at=datetime.now(UTC),
        )
        with pytest.raises(FrozenInstanceError):
            h.backend = "docker"  # type: ignore[misc]
