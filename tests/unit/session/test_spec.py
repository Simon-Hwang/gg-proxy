"""Tests for SessionSpec / PluginManifest / Decision."""
from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest

from gg_relay.session.spec import (
    Decision,
    PluginManifest,
    RuntimeHandle,
    SessionRuntimeContext,
    SessionSpec,
)


class TestPluginManifest:
    def test_profile_only(self):
        # Plan 2: --json is intentionally removed (upstream installer ignores it).
        m = PluginManifest(profile="minimal")
        assert m.to_install_argv() == ["--profile", "minimal", "--home", "/root"]

    def test_modules_only(self):
        m = PluginManifest(modules=("rules-python", "skills-security"))
        assert m.to_install_argv() == [
            "--modules", "rules-python,skills-security", "--home", "/root",
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

    def test_to_json_round_trip_minimal(self, tmp_path: Path):
        spec = SessionSpec(
            prompt="hi",
            cwd=tmp_path,
            plugins=PluginManifest(profile="minimal"),
        )
        restored = SessionSpec.from_json(spec.to_json())
        assert restored == spec

    def test_to_json_round_trip_complex(self, tmp_path: Path):
        spec = SessionSpec(
            prompt="run unit tests",
            cwd=tmp_path,
            plugins=PluginManifest(
                profile="python",
                modules=("rules-python", "skills-security"),
                skills=("brainstorming",),
                with_components=("lang:go",),
                without_components=("capability:learning",),
                extra_env=(("RELAY_TRACE_ID", "abc"), ("RELAY_X", "y")),
            ),
            executor="docker",
            timeout_s=900,
            metadata=(("user", "alice"), ("priority", "high")),
        )
        round_tripped = SessionSpec.from_json(spec.to_json())
        assert round_tripped == spec
        # cwd is rehydrated as Path, not str
        assert isinstance(round_tripped.cwd, Path)

    def test_to_json_excludes_runtime_secrets(self, tmp_path: Path):
        spec = SessionSpec(
            prompt="hi",
            cwd=tmp_path,
            plugins=PluginManifest(profile="minimal"),
        )
        payload = spec.to_json()
        # Defense-in-depth: spec serialisation NEVER carries credentials.
        # SessionRuntimeContext is injected via separate env keys
        # (ANTHROPIC_API_KEY=...), not encoded into spec_json.
        for forbidden in ("ANTHROPIC_API_KEY", "credentials"):
            assert forbidden not in payload

    def test_to_json_safe_returns_dict(self, tmp_path: Path):
        spec = SessionSpec(
            prompt="hello",
            cwd=tmp_path,
            plugins=PluginManifest(profile="minimal"),
            tags=("urgent", "alice"),
        )
        d = spec.to_json_safe()
        assert isinstance(d, dict)
        assert d["prompt"] == "hello"
        assert d["cwd"] == str(tmp_path)
        assert d["tags"] == ["urgent", "alice"]
        # hitl_policy intentionally absent so spec_json column never carries
        # class wiring.
        assert "hitl_policy" not in d

    def test_hitl_policy_round_trip_loses_policy_by_design(
        self, tmp_path: Path
    ):
        """Per Plan 4 D4.13, hitl_policy is host-side only and NOT persisted."""
        from gg_relay.session.hitl.policy import ToolPolicy

        spec = SessionSpec(
            prompt="hi",
            cwd=tmp_path,
            plugins=PluginManifest(profile="minimal"),
            hitl_policy=ToolPolicy(),
        )
        restored = SessionSpec.from_json(spec.to_json())
        assert restored.hitl_policy is None
        # Everything else should match.
        assert restored.prompt == spec.prompt
        assert restored.tags == spec.tags

    def test_tags_round_trip(self, tmp_path: Path):
        spec = SessionSpec(
            prompt="hi",
            cwd=tmp_path,
            plugins=PluginManifest(profile="minimal"),
            tags=("a", "b", "c"),
        )
        restored = SessionSpec.from_json(spec.to_json())
        assert restored.tags == ("a", "b", "c")


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


class TestSessionRuntimeContext:
    def test_default_empty(self):
        ctx = SessionRuntimeContext()
        assert dict(ctx.credentials) == {}
        assert ctx.trace_id == ""
        assert ctx.public_callback_base == ""

    def test_explicit_values(self):
        ctx = SessionRuntimeContext(
            credentials={"ANTHROPIC_API_KEY": "sk-xxx"},
            trace_id="trace-42",
            public_callback_base="https://relay.example.com",
        )
        assert ctx.credentials["ANTHROPIC_API_KEY"] == "sk-xxx"
        assert ctx.trace_id == "trace-42"
        assert ctx.public_callback_base == "https://relay.example.com"

    def test_frozen(self):
        from dataclasses import FrozenInstanceError

        ctx = SessionRuntimeContext()
        with pytest.raises(FrozenInstanceError):
            ctx.trace_id = "x"  # type: ignore[misc]

    def test_slots(self):
        # frozen+slots → no __dict__ → cannot set new attrs
        ctx = SessionRuntimeContext()
        with pytest.raises((AttributeError, TypeError)):
            ctx.brand_new = 1  # type: ignore[attr-defined]

    def test_default_credentials_shared_immutable(self):
        # Defending against the "mutable default" footgun: two instances
        # constructed without arguments must NOT share a mutable container that
        # leaks state between them.
        a = SessionRuntimeContext()
        b = SessionRuntimeContext()
        with pytest.raises(TypeError):
            a.credentials["k"] = "v"  # type: ignore[index]
        assert "k" not in b.credentials
