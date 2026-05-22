"""Tests for PluginAssembler / InstallShellAssembler (Plan 2 Task 1)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gg_relay.session.plugins import (
    InstallReport,
    InstallShellAssembler,
    PluginAssembler,
    PluginInstallError,
)
from gg_relay.session.spec import PluginManifest, SessionSpec


def _make_spec(
    tmp_path: Path,
    *,
    plugins: PluginManifest | None = None,
) -> SessionSpec:
    return SessionSpec(
        prompt="x",
        cwd=tmp_path,
        plugins=plugins or PluginManifest(profile="minimal"),
        executor="inprocess",
    )


def _write_install_state(install_dir: Path, **overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "schemaVersion": "gg.install.v1",
        "installedAt": "2026-05-22T00:00:00Z",
        "target": "claude",
        "installRoot": str(install_dir / ".claude"),
        "profileId": "minimal",
        "selectedModules": ["rules-core", "skills-core"],
        "includedComponents": [],
        "excludedComponents": [],
    }
    state.update(overrides)
    state_dir = install_dir / ".claude" / "gg"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "install-state.json").write_text(json.dumps(state), encoding="utf-8")
    return state


class _FakeProc:
    """Mock asyncio.subprocess.Process for create_subprocess_exec mocking."""

    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.communicate = AsyncMock(return_value=(stdout, stderr))


def test_init_raises_on_missing_install_sh(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="install.sh"):
        InstallShellAssembler(plugins_home=tmp_path)


def test_runtime_checkable_protocol(tmp_path: Path) -> None:
    (tmp_path / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    asm = InstallShellAssembler(plugins_home=tmp_path)
    assert isinstance(asm, PluginAssembler)


async def test_prepare_invokes_install_sh_with_correct_argv(tmp_path: Path) -> None:
    plugins_home = tmp_path / "gg-plugins"
    plugins_home.mkdir()
    (plugins_home / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    install_dir = tmp_path / "home"
    spec = _make_spec(
        tmp_path,
        plugins=PluginManifest(profile="minimal", modules=("rules-core",)),
    )

    captured: dict[str, Any] = {}

    async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        _write_install_state(install_dir)
        return _FakeProc(0)

    asm = InstallShellAssembler(plugins_home=plugins_home)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await asm.prepare(spec, install_dir=install_dir)

    argv = captured["argv"]
    assert argv[0] == str(plugins_home / "install.sh")
    assert "--profile" in argv and "minimal" in argv
    assert "--modules" in argv and "rules-core" in argv
    assert "--home" in argv and str(install_dir) in argv
    assert captured["kwargs"]["cwd"] == str(plugins_home)


async def test_prepare_returns_report_on_success(tmp_path: Path) -> None:
    plugins_home = tmp_path / "gg-plugins"
    plugins_home.mkdir()
    (plugins_home / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    install_dir = tmp_path / "home"
    spec = _make_spec(tmp_path)

    async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        _write_install_state(install_dir, selectedModules=["rules-core", "x-mod"])
        return _FakeProc(0)

    asm = InstallShellAssembler(plugins_home=plugins_home)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        report = await asm.prepare(spec, install_dir=install_dir)

    assert isinstance(report, InstallReport)
    assert report.schema_version == "gg.install.v1"
    assert report.profile_id == "minimal"
    assert report.selected_modules == ("rules-core", "x-mod")
    assert report.installed_at == "2026-05-22T00:00:00Z"
    assert report.duration_ms >= 0
    assert report.install_root == install_dir / ".claude"


async def test_prepare_raises_on_nonzero_exit(tmp_path: Path) -> None:
    plugins_home = tmp_path / "gg-plugins"
    plugins_home.mkdir()
    (plugins_home / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    install_dir = tmp_path / "home"
    spec = _make_spec(tmp_path)

    async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        return _FakeProc(2, stderr=b"profile not found: badname")

    asm = InstallShellAssembler(plugins_home=plugins_home)
    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        pytest.raises(PluginInstallError) as exc_info,
    ):
        await asm.prepare(spec, install_dir=install_dir)
    err = exc_info.value
    assert err.returncode == 2
    assert "profile not found" in err.stderr
    assert err.argv[0].endswith("install.sh")


async def test_prepare_raises_when_state_file_missing(tmp_path: Path) -> None:
    plugins_home = tmp_path / "gg-plugins"
    plugins_home.mkdir()
    (plugins_home / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    install_dir = tmp_path / "home"
    spec = _make_spec(tmp_path)

    async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        # install.sh exits 0 but writes NO state file
        return _FakeProc(0)

    asm = InstallShellAssembler(plugins_home=plugins_home)
    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        pytest.raises(PluginInstallError) as exc_info,
    ):
        await asm.prepare(spec, install_dir=install_dir)
    assert exc_info.value.returncode == 0
    assert "install-state.json" in exc_info.value.stderr


async def test_profile_only_manifest_argv(tmp_path: Path) -> None:
    plugins_home = tmp_path / "gg-plugins"
    plugins_home.mkdir()
    (plugins_home / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    install_dir = tmp_path / "home"
    spec = _make_spec(tmp_path, plugins=PluginManifest(profile="python"))

    captured: dict[str, Any] = {}

    async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        captured["argv"] = argv
        _write_install_state(install_dir, profileId="python")
        return _FakeProc(0)

    asm = InstallShellAssembler(plugins_home=plugins_home)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await asm.prepare(spec, install_dir=install_dir)
    argv = captured["argv"]
    assert "--profile" in argv
    assert "python" in argv
    assert "--modules" not in argv
    assert "--skills" not in argv


async def test_skills_modules_combined_argv(tmp_path: Path) -> None:
    plugins_home = tmp_path / "gg-plugins"
    plugins_home.mkdir()
    (plugins_home / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    install_dir = tmp_path / "home"
    spec = _make_spec(
        tmp_path,
        plugins=PluginManifest(
            modules=("m1", "m2"),
            skills=("s1",),
            with_components=("w1", "w2"),
            without_components=("x1",),
        ),
    )

    captured: dict[str, Any] = {}

    async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        captured["argv"] = argv
        _write_install_state(install_dir, profileId=None)
        return _FakeProc(0)

    asm = InstallShellAssembler(plugins_home=plugins_home)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await asm.prepare(spec, install_dir=install_dir)
    argv = captured["argv"]
    assert "--modules" in argv and "m1,m2" in argv
    assert "--skills" in argv and "s1" in argv
    assert argv.count("--with") == 2
    assert "w1" in argv and "w2" in argv
    assert "--without" in argv and "x1" in argv
    # --json must be GONE (Plan 2 decision: temporarily removed)
    assert "--json" not in argv


async def test_extra_env_propagated_to_subprocess(tmp_path: Path) -> None:
    plugins_home = tmp_path / "gg-plugins"
    plugins_home.mkdir()
    (plugins_home / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    install_dir = tmp_path / "home"
    spec = _make_spec(
        tmp_path,
        plugins=PluginManifest(
            profile="minimal",
            extra_env=(("GG_FOO", "bar"), ("GG_BAZ", "qux")),
        ),
    )

    captured: dict[str, Any] = {}

    async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        captured["env"] = kwargs.get("env")
        _write_install_state(install_dir)
        return _FakeProc(0)

    asm = InstallShellAssembler(plugins_home=plugins_home)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await asm.prepare(spec, install_dir=install_dir)
    env = captured["env"]
    assert env is not None
    assert env["GG_FOO"] == "bar"
    assert env["GG_BAZ"] == "qux"
    # Inherits PATH so install.sh can find node, npm, etc.
    assert "PATH" in env


async def test_to_install_argv_no_json_flag() -> None:
    """Plan 2 decision: --json is temporarily removed."""
    m = PluginManifest(profile="minimal")
    argv = m.to_install_argv(home_dir="/tmp/x")
    assert "--json" not in argv
    assert argv == ["--profile", "minimal", "--home", "/tmp/x"]
