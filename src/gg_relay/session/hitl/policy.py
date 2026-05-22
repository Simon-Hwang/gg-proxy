"""ToolPolicy — 工具类别 + 路径抽检的 HITL 决策。

文件类工具（Edit/Write/MultiEdit/NotebookEdit）：
  - 路径在 cwd 子树内 → ACCEPT
  - 路径越界或命中危险 pattern → NEEDS_HITL
  - 路径缺失 → NEEDS_HITL（路径强制集合 path_required_tools 内的工具）

HITL 工具（Bash/WebFetch/Task）→ 始终 NEEDS_HITL
中立工具（Read/Glob/Grep/LS）→ 始终 ACCEPT
未知工具 → NEEDS_HITL（保守）

`path_required_tools` 与 `auto_accept_tools` 解耦：
  - 默认两者一致（都是文件类四件套）
  - 调用方可以把非文件类工具（如 Bash）放进 auto_accept_tools，
    而不必同时放进 path_required_tools，从而不触发路径校验。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from gg_relay.session.spec import Decision

_DEFAULT_AUTO_ACCEPT = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_DEFAULT_HITL = frozenset({"Bash", "WebFetch", "Task"})
_DEFAULT_NEUTRAL = frozenset({"Read", "Glob", "Grep", "LS"})
_DEFAULT_PATH_REQUIRED = _DEFAULT_AUTO_ACCEPT
_DEFAULT_DANGEROUS: tuple[str, ...] = (
    "*.env",
    "*/.git/*",
    "*/secrets/*",
    "*/credentials/*",
    "*id_rsa*",
    "*.pem",
)

_PATH_FIELDS = ("file_path", "notebook_path", "path")


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolPolicy:
    auto_accept_tools: frozenset[str] = field(default=_DEFAULT_AUTO_ACCEPT)
    hitl_tools: frozenset[str] = field(default=_DEFAULT_HITL)
    neutral_tools: frozenset[str] = field(default=_DEFAULT_NEUTRAL)
    path_required_tools: frozenset[str] = field(default=_DEFAULT_PATH_REQUIRED)
    dangerous_patterns: tuple[str, ...] = field(default=_DEFAULT_DANGEROUS)

    def __post_init__(self) -> None:
        # Invariant: path checks only run for tools that are also auto-accepted,
        # so requiring a path on a tool outside auto_accept_tools is dead code
        # and almost certainly a misconfiguration. Catch it at construction.
        leak = self.path_required_tools - self.auto_accept_tools
        if leak:
            raise ValueError(
                "ToolPolicy invariant: path_required_tools must be a subset of "
                f"auto_accept_tools; offending tools: {sorted(leak)}"
            )

    def decide(self, tool: str, args: dict[str, Any], cwd: Path) -> Decision:
        if tool in self.neutral_tools:
            return Decision.ACCEPT
        if tool in self.auto_accept_tools:
            path = self._extract_path(args)
            if path is None:
                if tool in self.path_required_tools:
                    return Decision.NEEDS_HITL
                return Decision.ACCEPT
            if not self._inside_cwd(path, cwd):
                return Decision.NEEDS_HITL
            if self._matches_dangerous(path):
                return Decision.NEEDS_HITL
            return Decision.ACCEPT
        if tool in self.hitl_tools:
            return Decision.NEEDS_HITL
        return Decision.NEEDS_HITL

    @staticmethod
    def _extract_path(args: dict[str, Any]) -> Path | None:
        for k in _PATH_FIELDS:
            v = args.get(k)
            if isinstance(v, str) and v:
                return Path(v)
        return None

    @staticmethod
    def _inside_cwd(path: Path, cwd: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(cwd.resolve(strict=False))
            return True
        except ValueError:
            return False

    def _matches_dangerous(self, path: Path) -> bool:
        # 同时关闭两个旁路：
        #   I-1 symlink bypass — 用 resolved 路径而不是原始字符串（cwd 内的伪装链接
        #       innocent.txt → /work/.env 在 resolve 后才暴露危险后缀）
        #   I-2 case-sensitivity bypass — fnmatch 在 POSIX 大小写敏感，但 macOS/APFS、
        #       Windows/NTFS 视 .ENV == .env，必须双侧小写以兜底
        resolved = str(path.resolve(strict=False)).lower()
        return any(fnmatch(resolved, pat.lower()) for pat in self.dangerous_patterns)


DEFAULT_POLICY = ToolPolicy()
