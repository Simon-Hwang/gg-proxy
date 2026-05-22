"""Public data contracts: SessionSpec / PluginManifest / Decision / RuntimeHandle.

与 gg-plugins/install.sh CLI 严格对齐，避免抽象错位。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from gg_relay.session.transport.protocol import SessionTransport


class Decision(StrEnum):
    ACCEPT = "accept"
    # DENY = explicit refusal at the policy layer (e.g. category blacklist).
    # Distinct from the HITLCoordinator.resolve(decision="deny") string
    # literal, which represents the *user's* refusal after a tool.request.
    # Policy → Decision enum; HITL → str literal. Keep the two channels
    # separate even though both end in PermissionResultDeny.
    DENY = "deny"
    NEEDS_HITL = "needs_hitl"


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """声明本次 session 需要的 gg-plugins 资源。

    三种装配模式至少选一种（可叠加，install.sh 自行合并）：
      profile  — 5 个预设之一（minimal/core/go/python/full）
      modules  — 直接列举 module ID
      skills   — 按 skill 目录名挑选个别 skill
    """

    profile: Literal["minimal", "core", "go", "python", "full"] | None = None
    modules: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    with_components: tuple[str, ...] = ()
    without_components: tuple[str, ...] = ()
    extra_env: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not (self.profile or self.modules or self.skills):
            raise ValueError(
                "PluginManifest 必须指定 profile / modules / skills 至少一个"
            )

    def to_install_argv(self, home_dir: str = "/root") -> list[str]:
        """Render install.sh CLI argv.

        Plan 2 decision: ``--json`` is intentionally OMITTED. The gg-plugins
        install.sh currently does not implement that flag (it ignores it and
        writes plain logs to stdout), so passing it adds noise without value.
        Re-add when the upstream installer learns to emit structured output.
        """
        argv: list[str] = []
        if self.profile:
            argv += ["--profile", self.profile]
        if self.modules:
            argv += ["--modules", ",".join(self.modules)]
        if self.skills:
            argv += ["--skills", ",".join(self.skills)]
        for c in self.with_components:
            argv += ["--with", c]
        for c in self.without_components:
            argv += ["--without", c]
        argv += ["--home", home_dir]
        return argv


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """handler → SessionManager 的唯一入参。"""

    prompt: str
    cwd: Path
    plugins: PluginManifest
    executor: Literal["docker", "inprocess"] = "docker"
    timeout_s: int = 1800
    metadata: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeHandle:
    """ExecutorBackend.start() 返回值。后端无关。"""

    backend: str
    runtime_id: str
    transport: SessionTransport
    started_at: datetime
    extra: tuple[tuple[str, Any], ...] = ()
