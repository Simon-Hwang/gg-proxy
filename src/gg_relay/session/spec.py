"""Public data contracts: SessionSpec / PluginManifest / Decision / RuntimeHandle.

与 gg-plugins/install.sh CLI 严格对齐，避免抽象错位。
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from gg_relay.session.transport.protocol import SessionTransport


_EMPTY_MAPPING: Mapping[str, str] = MappingProxyType({})


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

    def to_json(self) -> str:
        """Serialise to a string suitable for ``GG_RELAY_SPEC_JSON`` env in the
        container. Includes ``plugins`` and ``metadata`` as nested objects;
        ``cwd`` becomes a string path.

        Round-trippable via ``SessionSpec.from_json()``. Does NOT include
        ``SessionRuntimeContext`` data — credentials are never serialised.
        """
        payload = {
            "prompt": self.prompt,
            "cwd": str(self.cwd),
            "plugins": {
                "profile": self.plugins.profile,
                "modules": list(self.plugins.modules),
                "skills": list(self.plugins.skills),
                "with_components": list(self.plugins.with_components),
                "without_components": list(self.plugins.without_components),
                "extra_env": [list(p) for p in self.plugins.extra_env],
            },
            "executor": self.executor,
            "timeout_s": self.timeout_s,
            "metadata": [list(p) for p in self.metadata],
        }
        return json.dumps(payload, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> SessionSpec:
        """Inverse of :meth:`to_json`."""
        data = cast(dict[str, Any], json.loads(raw))
        plugins_data = data["plugins"]
        plugins = PluginManifest(
            profile=plugins_data.get("profile"),
            modules=tuple(plugins_data.get("modules") or ()),
            skills=tuple(plugins_data.get("skills") or ()),
            with_components=tuple(plugins_data.get("with_components") or ()),
            without_components=tuple(plugins_data.get("without_components") or ()),
            extra_env=tuple(
                (k, v) for k, v in (plugins_data.get("extra_env") or [])
            ),
        )
        return cls(
            prompt=data["prompt"],
            cwd=Path(data["cwd"]),
            plugins=plugins,
            executor=data.get("executor", "docker"),
            timeout_s=int(data.get("timeout_s", 1800)),
            metadata=tuple((k, v) for k, v in (data.get("metadata") or [])),
        )


@dataclass(frozen=True, slots=True)
class SessionRuntimeContext:
    """Per-session runtime-only data.

    **NEVER** persisted, **NEVER** rendered to dashboards/IM, **NEVER** serialised
    into ``spec_json``. Injected by SessionManager into
    ``ExecutorBackend.start(spec, *, runtime_ctx)`` and never leaves runtime
    memory. Plan 4 D4.17 codifies the split between persistable ``SessionSpec``
    and ephemeral ``SessionRuntimeContext``; Plan 3 lands the class early because
    DockerExecutor.start() needs ``credentials`` to inject ``ANTHROPIC_API_KEY``
    into the container env.

    ``credentials`` defaults to an *immutable* empty mapping; supplying a plain
    ``dict`` is fine because we never mutate it — we only read it. The
    ``MappingProxyType`` default just guarantees that the shared default cannot
    accidentally be mutated by a caller and bleed into the next session.
    """

    credentials: Mapping[str, str] = field(default=_EMPTY_MAPPING)
    """Sensitive secrets injected into the runtime env (ANTHROPIC_API_KEY etc.).
    Never logged, never persisted."""

    trace_id: str = ""
    """OTel trace correlation id, threaded into ``RELAY_TRACE_ID`` env."""

    public_callback_base: str = ""
    """Base URL the runtime uses to build IM callback URLs (e.g. tool.approve)."""


@dataclass(frozen=True, slots=True)
class RuntimeHandle:
    """ExecutorBackend.start() 返回值。后端无关。"""

    backend: str
    runtime_id: str
    transport: SessionTransport
    started_at: datetime
    extra: tuple[tuple[str, Any], ...] = ()
