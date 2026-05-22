"""PluginAssembler Protocol + supporting types.

The assembler is the contract between SessionManager (host) and any concrete
plugin-installation strategy.  The current implementation
(InstallShellAssembler) shells out to gg-plugins/install.sh, but the Protocol
lets us swap in an in-process Node bridge, a remote cache fetcher, or a
no-op for tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from gg_relay.session.spec import SessionSpec


@dataclass(frozen=True, slots=True)
class InstallReport:
    """Outcome of a successful PluginAssembler.prepare() call.

    Parsed from ``<install_dir>/.claude/gg/install-state.json`` (schema
    ``gg.install.v1``) plus the assembler's own duration measurement.
    Immutable so the same report can be safely shipped to dashboards,
    IM cards, and the runner's ``install.done`` frame without defensive
    copying.
    """

    schema_version: str
    profile_id: str | None
    selected_modules: tuple[str, ...]
    included_components: tuple[str, ...]
    excluded_components: tuple[str, ...]
    install_root: Path
    installed_at: str
    duration_ms: int


class PluginInstallError(RuntimeError):
    """Raised by InstallShellAssembler when install.sh fails or its
    post-conditions are not met.

    ``returncode == 0`` paired with a missing state file is treated as a
    failure (the schema contract requires the state file to be written on
    every successful install).
    """

    def __init__(self, *, returncode: int, stderr: str, argv: tuple[str, ...]) -> None:
        super().__init__(f"install.sh exit {returncode}: {stderr[:512]}")
        self.returncode = returncode
        self.stderr = stderr
        self.argv = argv


@runtime_checkable
class PluginAssembler(Protocol):
    """Materialize the gg-plugins layout that the SDK will mount."""

    async def prepare(self, spec: SessionSpec, *, install_dir: Path) -> InstallReport: ...
