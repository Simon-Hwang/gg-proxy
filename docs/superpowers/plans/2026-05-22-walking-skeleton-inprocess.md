# Walking Skeleton — In-Process Backend with Real SDK

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 handler 用 `executor="inprocess"` 提交一个 `SessionSpec`，端到端跑通真
`claude-code-sdk` 调用，并验证 HITL 同步阻塞回路（auto-accept 文件类工具 / 其它 NEEDS_HITL
路径阻塞等待决策）。这是后续 Docker 后端 + IM 集成的最小可运行基线。

**Architecture:** 在 PLAN.md §5 的 `session/` 下新增 5 个子包；handler → SessionSpec →
InProcessExecutor → InMemoryTransport ↔ GgRelayClaudeClient → ClaudeSDKClient（注册
`can_use_tool` 回调到 ToolPolicy + HITLCoordinator）。本 plan 不做 Docker、不做 install.sh
装配、不接 PLAN.md SessionManager / EventBus / store（后续 Plan 2/3/4 增量补）。

**Tech Stack:** Python 3.12, `claude-code-sdk>=0.0.25`, `anyio`, `pytest` + `pytest-asyncio`,
`asyncio.Queue` 双向桥。

**Spec:** [`docs/superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md`]
(../specs/2026-05-22-sdk-bootstrap-and-runtime-design.md)

**Spike Report:** [`docs/sdk-spike-report.md`](../../sdk-spike-report.md)

---

## Pre-flight: 开发环境

- [ ] **Step 0.1: 准备 venv 并安装项目（editable）**

```bash
cd /data/workspace/github/gg-proxy
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Expected: 命令零退出码；`pip show gg-relay` 输出版本 `0.1.0`。

- [ ] **Step 0.2: 验证 claude-code-sdk 已可导入**

```bash
python -c "import claude_code_sdk; print(claude_code_sdk.__version__ if hasattr(claude_code_sdk,'__version__') else 'ok')"
```

Expected: 输出 `0.0.25` 或 `ok`。

---

## Task 1: 数据契约 `session/spec.py`

**Files:**
- Create: `src/gg_relay/session/spec.py`
- Create: `tests/unit/session/test_spec.py`

- [ ] **Step 1.1: 写失败测试**

写入 `tests/unit/session/test_spec.py`：

```python
"""Tests for SessionSpec / PluginManifest / Decision."""
from __future__ import annotations

import pytest
from pathlib import Path

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
        from datetime import datetime, timezone
        # 用 None placeholder transport 即可（只测 dataclass 行为）
        h = RuntimeHandle(
            backend="inprocess",
            runtime_id="task-1",
            transport=None,  # type: ignore[arg-type]
            started_at=datetime.now(timezone.utc),
        )
        with pytest.raises((AttributeError, Exception)):
            h.backend = "docker"  # type: ignore[misc]
```

- [ ] **Step 1.2: 跑测试确认失败**

```bash
pytest tests/unit/session/test_spec.py -v
```

Expected: `ModuleNotFoundError: No module named 'gg_relay.session.spec'`

- [ ] **Step 1.3: 实现 `spec.py`**

写入 `src/gg_relay/session/spec.py`：

```python
"""Public data contracts: SessionSpec / PluginManifest / Decision / RuntimeHandle.

与 gg-plugins/install.sh CLI 严格对齐，避免抽象错位。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, TYPE_CHECKING

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
        argv += ["--home", home_dir, "--json"]
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
    transport: "SessionTransport"
    started_at: datetime
    extra: tuple[tuple[str, Any], ...] = ()
```

- [ ] **Step 1.4: 跑测试确认通过**

```bash
pytest tests/unit/session/test_spec.py -v
```

Expected: 6 passed.

- [ ] **Step 1.5: 提交**

```bash
git add src/gg_relay/session/spec.py tests/unit/session/test_spec.py
git commit -m "feat(session): SessionSpec / PluginManifest / Decision data contracts

与 gg-plugins/install.sh CLI 1:1 对齐：
- PluginManifest.to_install_argv() 翻译为 install.sh argv
- 三模式（profile/modules/skills）至少一个的校验
- frozen dataclass 保证不可变"
```

---

## Task 2: Transport 协议与帧定义 `session/transport/protocol.py`

**Files:**
- Create: `src/gg_relay/session/transport/__init__.py`
- Create: `src/gg_relay/session/transport/protocol.py`

> 此 task 无独立测试（纯类型定义）；其行为由 Task 3 InMemoryTransport 的测试覆盖。

- [ ] **Step 2.1: 创建包 + Protocol 文件**

写入 `src/gg_relay/session/transport/__init__.py`：

```python
"""Bidirectional JSONL transport between host and runner.

InMemoryTransport is for in-process backend; UnixSocketTransport (Plan 3) is for Docker.
Both implement SessionTransport Protocol.
"""
from gg_relay.session.transport.protocol import (
    ControlFrame,
    EventFrame,
    SessionTransport,
    TransportClosed,
)

__all__ = ["ControlFrame", "EventFrame", "SessionTransport", "TransportClosed"]
```

写入 `src/gg_relay/session/transport/protocol.py`：

```python
"""SessionTransport Protocol + frame TypedDicts.

帧设计参考 spec §6.2：
  容器 → 宿主 (EventFrame): install.done | msg.chunk | tool.request | tool.result
                            | session.end | error | pong
  宿主 → 容器 (ControlFrame): tool.decision | interrupt | shutdown | ping
"""
from __future__ import annotations

from typing import Any, Literal, NotRequired, Protocol, TypedDict, runtime_checkable


# ── Event frames (runner → host) ──────────────────────────────────────────

class _BaseFrame(TypedDict):
    v: int          # protocol version, currently 1
    type: str
    seq: int        # monotonic per-direction
    ts: str         # ISO8601 UTC


class InstallDoneFrame(_BaseFrame):
    state: dict[str, Any]   # gg-plugins install state JSON


class MsgChunkFrame(_BaseFrame):
    data: dict[str, Any]    # SDK message chunk (TextBlock / ToolUseBlock / etc serialized)


class ToolRequestFrame(_BaseFrame):
    req_id: str
    tool: str
    args: dict[str, Any]


class ToolResultFrame(_BaseFrame):
    req_id: str
    ok: bool
    result: NotRequired[dict[str, Any]]
    error: NotRequired[str]


class SessionEndFrame(_BaseFrame):
    status: Literal["completed", "cancelled", "crashed"]
    tokens: NotRequired[dict[str, int]]
    cost_usd: NotRequired[float]


class ErrorFrame(_BaseFrame):
    code: str
    message: str
    traceback: NotRequired[str]


class PongFrame(_BaseFrame):
    pass


EventFrame = (
    InstallDoneFrame
    | MsgChunkFrame
    | ToolRequestFrame
    | ToolResultFrame
    | SessionEndFrame
    | ErrorFrame
    | PongFrame
)


# ── Control frames (host → runner) ────────────────────────────────────────

class ToolDecisionFrame(_BaseFrame):
    req_id: str
    decision: Literal["accept", "deny"]
    reason: NotRequired[str]


class InterruptFrame(_BaseFrame):
    pass


class ShutdownFrame(_BaseFrame):
    pass


class PingFrame(_BaseFrame):
    pass


ControlFrame = ToolDecisionFrame | InterruptFrame | ShutdownFrame | PingFrame


# ── Exceptions ────────────────────────────────────────────────────────────

class TransportClosed(Exception):
    """Raised when send/recv is called on a closed transport."""


# ── Protocol ──────────────────────────────────────────────────────────────

@runtime_checkable
class SessionTransport(Protocol):
    """Bidirectional JSONL stream. Single connection, long-lived."""

    @property
    def is_alive(self) -> bool: ...
    async def send(self, frame: ControlFrame) -> None: ...
    async def recv(self) -> EventFrame: ...
    async def close(self) -> None: ...
```

- [ ] **Step 2.2: 编译检查（mypy 严格模式）**

```bash
mypy src/gg_relay/session/transport/
```

Expected: `Success: no issues found in 2 source files`

- [ ] **Step 2.3: 提交**

```bash
git add src/gg_relay/session/transport/
git commit -m "feat(transport): SessionTransport Protocol + frame TypedDicts

帧定义参考 spec §6.2:
- 容器→宿主 7 种 EventFrame
- 宿主→容器 4 种 ControlFrame
- 所有帧共享 {v,type,seq,ts} 信封"
```

---

## Task 3: `InMemoryTransport` 实现

**Files:**
- Create: `src/gg_relay/session/transport/inmemory.py`
- Create: `tests/unit/session/__init__.py` (if not exists)
- Create: `tests/unit/session/test_transport_inmemory.py`

> **Updated by Task 7 (commit `25c4f65`):** the eager `_alive` early-return in `recv()` was removed to support drain-then-close (spec §6.4). The sentinel branch is now the sole close-detection path.

- [ ] **Step 3.1: 写失败测试**

写入 `tests/unit/session/test_transport_inmemory.py`：

```python
"""Tests for InMemoryTransport."""
from __future__ import annotations

import asyncio

import pytest

from gg_relay.session.transport import TransportClosed
from gg_relay.session.transport.inmemory import InMemoryTransport, make_pair


def _ping() -> dict:
    return {"v": 1, "type": "ping", "seq": 0, "ts": "2026-01-01T00:00:00Z"}


def _pong(seq: int = 0) -> dict:
    return {"v": 1, "type": "pong", "seq": seq, "ts": "2026-01-01T00:00:00Z"}


class TestInMemoryTransportPair:
    async def test_send_recv_roundtrip(self):
        host, runner = make_pair()
        await host.send(_ping())  # type: ignore[arg-type]
        frame = await runner.recv()
        assert frame["type"] == "ping"

        await runner.send(_pong(seq=1))  # type: ignore[arg-type]
        frame = await host.recv()
        assert frame["type"] == "pong"
        assert frame["seq"] == 1

    async def test_close_propagates(self):
        host, runner = make_pair()
        await host.close()
        assert host.is_alive is False
        assert runner.is_alive is False
        with pytest.raises(TransportClosed):
            await host.send(_ping())  # type: ignore[arg-type]
        with pytest.raises(TransportClosed):
            await runner.recv()

    async def test_recv_blocks_until_send(self):
        host, runner = make_pair()

        async def delayed_send():
            await asyncio.sleep(0.01)
            await host.send(_ping())  # type: ignore[arg-type]

        task = asyncio.create_task(delayed_send())
        frame = await asyncio.wait_for(runner.recv(), timeout=0.5)
        await task
        assert frame["type"] == "ping"

    async def test_send_recv_ordering(self):
        host, runner = make_pair()
        for i in range(5):
            await host.send(_pong(seq=i))  # type: ignore[arg-type]
        seqs = []
        for _ in range(5):
            f = await runner.recv()
            seqs.append(f["seq"])
        assert seqs == [0, 1, 2, 3, 4]
```

写入 `src/gg_relay/session/hitl/__init__.py` 占位（Task 4 才会用）— **现在先跳过这步**。

> 如果 `tests/unit/session/__init__.py` 不存在，建一个空文件：
> `touch tests/unit/session/__init__.py`

- [ ] **Step 3.2: 跑测试确认失败**

```bash
pytest tests/unit/session/test_transport_inmemory.py -v
```

Expected: `ImportError: cannot import name 'InMemoryTransport'`

- [ ] **Step 3.3: 实现 `inmemory.py`**

写入 `src/gg_relay/session/transport/inmemory.py`：

```python
"""InMemoryTransport — for InProcessExecutor.

Two coupled queues: outbound from one side is inbound to the other.
make_pair() returns (host_side, runner_side).
"""
from __future__ import annotations

import asyncio
from typing import cast

from gg_relay.session.transport.protocol import (
    ControlFrame,
    EventFrame,
    TransportClosed,
)

_CLOSE_SENTINEL: object = object()


class InMemoryTransport:
    """Implements SessionTransport with two asyncio.Queue.

    send() writes to outbound; recv() reads from inbound.
    Closing propagates to the paired transport via the sentinel.
    """

    def __init__(
        self,
        inbound: asyncio.Queue[object],
        outbound: asyncio.Queue[object],
        paired: "InMemoryTransport | None" = None,
    ) -> None:
        self._inbound = inbound
        self._outbound = outbound
        self._paired = paired
        self._alive = True

    @property
    def is_alive(self) -> bool:
        return self._alive

    async def send(self, frame: ControlFrame | EventFrame) -> None:
        if not self._alive:
            raise TransportClosed("transport closed")
        await self._outbound.put(frame)

    async def recv(self) -> EventFrame:
        if not self._alive:
            raise TransportClosed("transport closed")
        item = await self._inbound.get()
        if item is _CLOSE_SENTINEL:
            self._alive = False
            raise TransportClosed("peer closed")
        return cast(EventFrame, item)

    async def close(self) -> None:
        if not self._alive:
            return
        self._alive = False
        await self._outbound.put(_CLOSE_SENTINEL)
        if self._paired is not None and self._paired._alive:
            await self._paired.close()


def make_pair(
    maxsize: int = 1024,
) -> tuple[InMemoryTransport, InMemoryTransport]:
    """Return (host_side, runner_side) — frames sent by host arrive at runner.recv."""
    q_h2r: asyncio.Queue[object] = asyncio.Queue(maxsize=maxsize)
    q_r2h: asyncio.Queue[object] = asyncio.Queue(maxsize=maxsize)
    host = InMemoryTransport(inbound=q_r2h, outbound=q_h2r)
    runner = InMemoryTransport(inbound=q_h2r, outbound=q_r2h, paired=host)
    host._paired = runner
    return host, runner
```

- [ ] **Step 3.4: 跑测试确认通过**

```bash
pytest tests/unit/session/test_transport_inmemory.py -v
```

Expected: 4 passed.

- [ ] **Step 3.5: 提交**

```bash
git add src/gg_relay/session/transport/inmemory.py tests/unit/session/test_transport_inmemory.py
git commit -m "feat(transport): InMemoryTransport pair for in-process backend

- make_pair() 返回 (host_side, runner_side) 双向桥
- close() 通过 sentinel 传播到对端
- 不带心跳（无需要），符合 spec §5.2"
```

---

## Task 4: `ToolPolicy` HITL 策略 `hitl/policy.py`

**Files:**
- Create: `src/gg_relay/session/hitl/__init__.py`
- Create: `src/gg_relay/session/hitl/policy.py`
- Create: `tests/unit/session/test_policy.py`

- [ ] **Step 4.1: 写失败测试**

写入 `tests/unit/session/test_policy.py`：

```python
"""Tests for ToolPolicy."""
from __future__ import annotations

from pathlib import Path

import pytest

from gg_relay.session.hitl.policy import DEFAULT_POLICY, ToolPolicy
from gg_relay.session.spec import Decision


class TestNeutralTools:
    @pytest.mark.parametrize("tool", ["Read", "Glob", "Grep", "LS"])
    def test_neutral_always_accept(self, tool: str):
        assert DEFAULT_POLICY.decide(tool, {}, Path("/work")) == Decision.ACCEPT


class TestAutoAcceptFileTools:
    @pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
    def test_inside_cwd_accept(self, tool: str):
        d = DEFAULT_POLICY.decide(
            tool, {"file_path": "/work/src/main.py"}, Path("/work")
        )
        assert d == Decision.ACCEPT

    def test_outside_cwd_needs_hitl(self):
        d = DEFAULT_POLICY.decide(
            "Write", {"file_path": "/etc/passwd"}, Path("/work")
        )
        assert d == Decision.NEEDS_HITL

    @pytest.mark.parametrize("path", [
        "/work/.env",
        "/work/secrets/db.json",
        "/work/.git/config",
        "/work/id_rsa",
        "/work/cert.pem",
    ])
    def test_dangerous_pattern_needs_hitl(self, path: str):
        d = DEFAULT_POLICY.decide("Write", {"file_path": path}, Path("/work"))
        assert d == Decision.NEEDS_HITL

    @pytest.mark.parametrize("args", [
        {},
        {"file_path": ""},
        {"file_path": None},
        {"notebook_path": ""},
        {"path": ""},
        {"file_path": "", "notebook_path": "", "path": ""},
    ])
    def test_missing_or_empty_path_needs_hitl(self, args):
        """Path-required tools without a usable path string trigger HITL.

        Empty strings and None must be treated identically to a missing key —
        otherwise an SDK that passes `file_path=""` would bypass the path
        scoping check entirely.
        """
        d = DEFAULT_POLICY.decide("Write", args, Path("/work"))
        assert d == Decision.NEEDS_HITL

    def test_dangerous_pattern_case_insensitive(self, tmp_path: Path):
        """Uppercase variants of dangerous filenames must still trigger HITL.

        Fixes case-sensitivity bypass: .ENV / ID_RSA / .PEM are equivalent on
        macOS/Windows filesystems and must not slip through.
        """
        d = DEFAULT_POLICY.decide(
            "Write", {"file_path": str(tmp_path / "config.ENV")}, tmp_path
        )
        assert d == Decision.NEEDS_HITL

    def test_dangerous_pattern_via_symlink(self, tmp_path: Path):
        """A cwd-internal symlink pointing at a dangerous target must trigger HITL.

        Fixes symlink-bypass: an innocuous-looking name like 'innocent.txt'
        that resolves to '/work/.env' must be matched on the resolved path.
        """
        dangerous = tmp_path / ".env"
        dangerous.write_text("SECRET=x")
        link = tmp_path / "innocent.txt"
        link.symlink_to(dangerous)
        d = DEFAULT_POLICY.decide(
            "Write", {"file_path": str(link)}, tmp_path
        )
        assert d == Decision.NEEDS_HITL


class TestHITLTools:
    @pytest.mark.parametrize("tool", ["Bash", "WebFetch", "Task"])
    def test_always_hitl(self, tool: str):
        assert DEFAULT_POLICY.decide(tool, {}, Path("/work")) == Decision.NEEDS_HITL


class TestUnknownTools:
    def test_unknown_tool_conservative(self):
        assert DEFAULT_POLICY.decide("FrobTheBaz", {}, Path("/work")) == Decision.NEEDS_HITL


class TestPolicyOverride:
    def test_custom_policy_can_widen_auto_accept(self):
        custom = ToolPolicy(
            auto_accept_tools=frozenset({"Bash"}),
            hitl_tools=frozenset(),
            neutral_tools=frozenset(),
            # Bash isn't a file tool, so don't require a path. Must be a subset
            # of auto_accept_tools per ToolPolicy.__post_init__ invariant.
            path_required_tools=frozenset(),
            dangerous_patterns=(),
        )
        # Bash + no path-check (since no file_path key) → ACCEPT
        assert custom.decide("Bash", {"command": "ls"}, Path("/work")) == Decision.ACCEPT

    def test_invariant_rejects_leaked_path_required(self):
        """path_required_tools must be a subset of auto_accept_tools."""
        with pytest.raises(ValueError, match="path_required_tools"):
            ToolPolicy(
                auto_accept_tools=frozenset({"Edit"}),
                path_required_tools=frozenset({"Edit", "Bash"}),
            )
```

- [ ] **Step 4.2: 跑测试确认失败**

```bash
pytest tests/unit/session/test_policy.py -v
```

Expected: `ModuleNotFoundError: No module named 'gg_relay.session.hitl'`

- [ ] **Step 4.3: 实现 policy.py**

写入 `src/gg_relay/session/hitl/__init__.py`：

```python
"""HITL (Human-In-The-Loop) policy and coordination."""
from gg_relay.session.hitl.policy import DEFAULT_POLICY, ToolPolicy

__all__ = ["DEFAULT_POLICY", "ToolPolicy"]
```

写入 `src/gg_relay/session/hitl/policy.py`：

```python
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
```

- [ ] **Step 4.4: 跑测试确认通过**

```bash
pytest tests/unit/session/test_policy.py -v
```

Expected: 22 passed (4 + 4 + 1 + 5 + 1 + 1 + 1 + 3 + 1 + 1 — parametrized cases each count;
含 case-insensitive 与 symlink-bypass 两条安全回归测试).

- [ ] **Step 4.5: 提交**

```bash
git add src/gg_relay/session/hitl/ tests/unit/session/test_policy.py
git commit -m "feat(hitl): ToolPolicy with category + path scoping

- 4 类文件工具默认 auto-accept（路径必须在 cwd 子树内 & 不命中危险 pattern）
- Bash/WebFetch/Task 始终 NEEDS_HITL
- Read/Glob/Grep/LS 中立 ACCEPT
- 未知工具保守 NEEDS_HITL
- ToolPolicy 是 frozen dataclass，调用方可 override 任何字段"
```

---

## Task 5: `HITLCoordinator` 决策路由 `hitl/coordinator.py`

**Files:**
- Create: `src/gg_relay/session/hitl/coordinator.py`
- Modify: `src/gg_relay/session/hitl/__init__.py`
- Create: `tests/unit/session/test_hitl_coordinator.py`

- [ ] **Step 5.1: 写失败测试**

写入 `tests/unit/session/test_hitl_coordinator.py`：

```python
"""Tests for HITLCoordinator."""
from __future__ import annotations

import asyncio

import pytest

from gg_relay.session.hitl.coordinator import HITLCoordinator, HITLNotPending


class TestHITLCoordinator:
    async def test_request_and_approve(self):
        coord = HITLCoordinator()

        async def approver():
            await asyncio.sleep(0.01)
            await coord.resolve("req-1", "accept", reason=None)

        task = asyncio.create_task(approver())
        decision = await asyncio.wait_for(
            coord.request("req-1", tool="Bash", args={"command": "ls"}),
            timeout=0.5,
        )
        await task
        assert decision == "accept"

    async def test_request_and_deny(self):
        coord = HITLCoordinator()
        asyncio.create_task(coord.resolve("req-2", "deny", reason="not safe"))
        decision = await coord.request("req-2", tool="Bash", args={})
        assert decision == "deny"

    async def test_resolve_unknown_req_raises(self):
        coord = HITLCoordinator()
        with pytest.raises(HITLNotPending):
            await coord.resolve("nope", "accept")

    async def test_resolve_after_completion_raises(self):
        """After request() returns, the entry is popped; a stale resolve raises."""
        coord = HITLCoordinator()
        asyncio.create_task(coord.resolve("req-3", "accept"))
        await coord.request("req-3", tool="Bash", args={})
        # req-3 已被 request() pop 出 _pending，再 resolve 应当抛 HITLNotPending
        with pytest.raises(HITLNotPending):
            await coord.resolve("req-3", "accept")

    async def test_concurrent_requests(self):
        coord = HITLCoordinator()

        async def approve_after(req_id: str, delay: float):
            await asyncio.sleep(delay)
            await coord.resolve(req_id, "accept")

        asyncio.create_task(approve_after("a", 0.01))
        asyncio.create_task(approve_after("b", 0.02))

        results = await asyncio.gather(
            coord.request("a", tool="Bash", args={}),
            coord.request("b", tool="WebFetch", args={}),
        )
        assert results == ["accept", "accept"]

    async def test_pending_snapshot(self):
        coord = HITLCoordinator()
        t1 = asyncio.create_task(coord.request("p1", tool="Bash", args={"cmd": "x"}))
        await asyncio.sleep(0)  # let request register
        snap = coord.pending_snapshot()
        assert "p1" in snap
        assert snap["p1"]["tool"] == "Bash"
        await coord.resolve("p1", "accept")
        await t1
```

- [ ] **Step 5.2: 跑测试确认失败**

```bash
pytest tests/unit/session/test_hitl_coordinator.py -v
```

Expected: `ImportError: cannot import name 'HITLCoordinator'`

- [ ] **Step 5.3: 实现 coordinator.py**

写入 `src/gg_relay/session/hitl/coordinator.py`：

```python
"""HITLCoordinator — pending-future router for HITL decisions.

A single coordinator serves an entire process; request(req_id) blocks until
resolve(req_id, decision) is called from elsewhere (REST endpoint, IM callback,
or test scaffold).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, cast


class HITLNotPending(LookupError):
    """resolve() called for a req_id not currently pending."""


@dataclass(frozen=True, slots=True)
class _PendingEntry:
    tool: str
    args: dict[str, Any]
    future: asyncio.Future[tuple[str, str | None]]


class HITLCoordinator:
    """Stores pending HITL requests by req_id; resolve() wakes the awaiter."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingEntry] = {}
        self._lock = asyncio.Lock()

    async def request(
        self,
        req_id: str,
        *,
        tool: str,
        args: dict[str, Any],
    ) -> Literal["accept", "deny"]:
        """Register req_id and block until resolve() is called."""
        async with self._lock:
            if req_id in self._pending:
                raise ValueError(f"req_id {req_id!r} already pending")
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[tuple[str, str | None]] = loop.create_future()
            self._pending[req_id] = _PendingEntry(tool=tool, args=args, future=fut)

        try:
            decision, _reason = await fut
        finally:
            async with self._lock:
                self._pending.pop(req_id, None)
        return cast(Literal["accept", "deny"], decision)

    async def resolve(
        self,
        req_id: str,
        decision: Literal["accept", "deny"],
        reason: str | None = None,
    ) -> None:
        """Wake the request(req_id) coroutine with decision."""
        async with self._lock:
            entry = self._pending.get(req_id)
            if entry is None or entry.future.done():
                raise HITLNotPending(req_id)
            entry.future.set_result((decision, reason))

    def pending_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a snapshot of all currently-pending requests.

        Returns a shallow-defensive-copy so mutating the returned structure
        cannot affect runner state. Safe to publish to dashboards / IM
        cards (which may inadvertently modify their input).

        Note: ``dict(e.args)`` is a shallow copy — sufficient because ``args``
        values are expected to be scalars per spec §6.2 (strings, ints, paths).
        If a caller stores nested dicts/lists in ``args``, they would still
        see mutations propagate one level deep; deep-copy is deferred until
        Plan 4 needs it.

        Must be called from the same event loop thread that owns this
        coordinator. Safe on CPython under single-threaded asyncio (dict
        comprehension is atomic at the bytecode level); not safe under
        threadpool/PyPy/multi-loop scenarios.
        """
        return {
            rid: {"tool": e.tool, "args": dict(e.args)}
            for rid, e in self._pending.items()
            if not e.future.done()
        }
```

> **Plan-template sync (final-review I-5):** ``pending_snapshot`` now returns a
> shallow-defensive-copy of the per-entry ``args`` dict so dashboards / IM
> cards mutating the returned structure cannot poison runner state. Regression
> test ``test_pending_snapshot_isolation`` covers it.

更新 `src/gg_relay/session/hitl/__init__.py`：

```python
"""HITL (Human-In-The-Loop) policy and coordination."""
from gg_relay.session.hitl.coordinator import HITLCoordinator, HITLNotPending
from gg_relay.session.hitl.policy import DEFAULT_POLICY, ToolPolicy

__all__ = ["DEFAULT_POLICY", "HITLCoordinator", "HITLNotPending", "ToolPolicy"]
```

- [ ] **Step 5.4: 跑测试确认通过**

```bash
pytest tests/unit/session/test_hitl_coordinator.py -v
```

Expected: 6 passed.

- [ ] **Step 5.5: 提交**

```bash
git add src/gg_relay/session/hitl/coordinator.py src/gg_relay/session/hitl/__init__.py tests/unit/session/test_hitl_coordinator.py
git commit -m "feat(hitl): HITLCoordinator with pending-future routing

- request(req_id) 注册并阻塞，等待 resolve()
- resolve(req_id, decision) 唤醒对应 future
- pending_snapshot() 供 dashboard / IM 查询当前等审项"
```

---

## Task 6: `ExecutorBackend` Protocol

**Files:**
- Create: `src/gg_relay/session/executor/__init__.py`
- Create: `src/gg_relay/session/executor/protocol.py`

> 纯类型定义，无独立测试；由 Task 7 InProcessExecutor 测试覆盖。

- [ ] **Step 6.1: 创建 Protocol 文件**

写入 `src/gg_relay/session/executor/__init__.py`：

```python
"""ExecutorBackend implementations."""
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.executor.protocol import ExecutorBackend, RunnerFn

__all__ = ["ExecutorBackend", "InProcessExecutor", "RunnerFn"]
```

写入 `src/gg_relay/session/executor/protocol.py`：

```python
"""ExecutorBackend Protocol — abstracts in-process vs. docker vs. (future) k8s."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from gg_relay.session.spec import RuntimeHandle, SessionSpec
from gg_relay.session.transport.inmemory import InMemoryTransport

RunnerFn = Callable[[InMemoryTransport, SessionSpec], Awaitable[None]]
"""Runner coroutine signature for InProcessExecutor.

Canonical home for the type alias (the executor's contract). Re-exported
from ``executor/inprocess.py`` and ``client.py`` for back-compat with
existing call sites.

CONTRACT (cooperative cancellation):
- The runner MUST have at least one ``await`` point so ``stop()``'s
  ``task.cancel()`` can land. A runner that does ``while True: pass`` will
  hang ``stop()`` indefinitely because asyncio can't preempt non-yielding
  coroutines.
- When the runner returns or raises, ``runner_wrapper.finally`` closes the
  runner-side transport, which propagates a close sentinel to the host side.
- For Task 8+ (real SDK runner): exceptions inside the runner should be
  surfaced to the host via an ``error`` event frame before re-raising, so
  the host can observe the root cause beyond just ``TransportClosed``.
"""


@runtime_checkable
class ExecutorBackend(Protocol):
    """Lifecycle: start() returns a ready-to-use RuntimeHandle holding a
    bidirectional transport. stop() tears down. health() probes liveness.

    The backend MUST NOT participate in event streaming; it only owns the
    runtime (container/coroutine/pod) and the transport handle.
    """

    async def start(self, spec: SessionSpec) -> RuntimeHandle: ...
    async def stop(self, handle: RuntimeHandle) -> None: ...
    async def health(self, handle: RuntimeHandle) -> bool: ...
```

> **Plan-template sync (final-review I-3):** ``RunnerFn`` is now defined
> canonically in ``executor/protocol.py`` (it IS the executor's contract).
> Both ``executor/inprocess.py`` and ``client.py`` re-export it for
> back-compat. ``session/executor/__init__.py`` also surfaces
> ``InProcessExecutor`` and ``RunnerFn`` alongside the protocol so downstream
> imports can stay shallow.

- [ ] **Step 6.2: 编译检查**

```bash
mypy src/gg_relay/session/executor/
```

Expected: `Success: no issues found in 2 source files`

- [ ] **Step 6.3: 提交**

```bash
git add src/gg_relay/session/executor/
git commit -m "feat(executor): ExecutorBackend Protocol

后端无关接口：start() 返回 RuntimeHandle + 已就绪的 SessionTransport，
stop() 销毁运行时，health() 探活。后端不参与事件流。"
```

---

## Task 7: `InProcessExecutor` — 不接真 SDK 版（mock runner 用于先测后端骨架）

**Files:**
- Create: `src/gg_relay/session/executor/inprocess.py`
- Create: `tests/unit/session/test_executor_inprocess.py`

> 注意：本 task 实现的 InProcessExecutor 只起一个 **mock runner coroutine**，
> 用于先验证后端 + transport 拼接正确。真 SDK 桥接在 Task 8 (client.py) 里完成。

- [ ] **Step 7.1: 写失败测试**

写入 `tests/unit/session/test_executor_inprocess.py`：

```python
"""Tests for InProcessExecutor (with stub runner)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.spec import PluginManifest, SessionSpec
from gg_relay.session.transport.inmemory import InMemoryTransport


async def _stub_runner(transport: InMemoryTransport, spec: SessionSpec) -> None:
    """A stub runner that just echoes one msg.chunk and ends."""
    await transport.send({  # type: ignore[arg-type]
        "v": 1, "type": "msg.chunk", "seq": 0, "ts": "2026-01-01T00:00:00Z",
        "data": {"prompt": spec.prompt},
    })
    await transport.send({  # type: ignore[arg-type]
        "v": 1, "type": "session.end", "seq": 1, "ts": "2026-01-01T00:00:00Z",
        "status": "completed",
    })


async def _drain_until_closed(transport: InMemoryTransport):
    """Async generator that yields frames until TransportClosed (spec §6.4 drain semantics)."""
    from gg_relay.session.transport import TransportClosed
    try:
        while True:
            yield await transport.recv()
    except TransportClosed:
        return


class TestInProcessExecutor:
    async def test_start_returns_handle(self, tmp_path: Path):
        exec_ = InProcessExecutor(runner=_stub_runner)
        spec = SessionSpec(
            prompt="hello",
            cwd=tmp_path,
            plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )
        handle = await exec_.start(spec)
        assert handle.backend == "inprocess"
        assert handle.transport.is_alive
        frames = []
        for _ in range(2):
            frames.append(await handle.transport.recv())
        assert frames[0]["type"] == "msg.chunk"
        assert frames[1]["type"] == "session.end"
        await exec_.stop(handle)

    async def test_stop_closes_transport(self, tmp_path: Path):
        exec_ = InProcessExecutor(runner=_stub_runner)
        spec = SessionSpec(
            prompt="hi", cwd=tmp_path, plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )
        handle = await exec_.start(spec)
        await exec_.stop(handle)
        assert handle.transport.is_alive is False
        assert await exec_.health(handle) is False

    async def test_runner_exception_propagates(self, tmp_path: Path):
        async def bad_runner(transport: InMemoryTransport, spec: SessionSpec) -> None:
            raise RuntimeError("boom")

        exec_ = InProcessExecutor(runner=bad_runner)
        spec = SessionSpec(
            prompt="hi", cwd=tmp_path, plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )
        handle = await exec_.start(spec)
        # runner_wrapper closes runner_side in finally → host sees TransportClosed.
        from gg_relay.session.transport import TransportClosed
        with pytest.raises(TransportClosed):
            await handle.transport.recv()
        await exec_.stop(handle)

    async def test_stop_after_runner_finished_natural(self, tmp_path: Path):
        """stop() on a runner that already returned naturally should be a no-op for cancel."""
        exec_ = InProcessExecutor(runner=_stub_runner)
        spec = SessionSpec(
            prompt="hi", cwd=tmp_path, plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )
        handle = await exec_.start(spec)
        async for _ in _drain_until_closed(handle.transport):
            pass
        # _tasks should be auto-cleaned by add_done_callback after a scheduling round
        await asyncio.sleep(0)
        assert handle.runtime_id not in exec_._tasks
        await exec_.stop(handle)
        assert handle.transport.is_alive is False

    async def test_stop_unknown_handle_is_noop(self, tmp_path: Path):
        """stop() on a handle whose runtime_id is not in _tasks is idempotent."""
        exec_ = InProcessExecutor(runner=_stub_runner)
        spec = SessionSpec(
            prompt="x", cwd=tmp_path, plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )
        handle = await exec_.start(spec)
        exec_._tasks.pop(handle.runtime_id, None)
        await exec_.stop(handle)
        assert handle.transport.is_alive is False

    async def test_concurrent_starts_independent(self, tmp_path: Path):
        """Two start() calls should yield two independent runtime_ids and transports."""
        exec_ = InProcessExecutor(runner=_stub_runner)
        spec = SessionSpec(
            prompt="hi", cwd=tmp_path, plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )
        h1, h2 = await asyncio.gather(exec_.start(spec), exec_.start(spec))
        assert h1.runtime_id != h2.runtime_id
        assert h1.transport is not h2.transport
        async for _ in _drain_until_closed(h1.transport):
            pass
        async for _ in _drain_until_closed(h2.transport):
            pass
        await exec_.stop(h1)
        await exec_.stop(h2)

    async def test_drain_after_runner_close(self, tmp_path: Path):
        """Buffered frames sent before close must be drainable on the host side (spec §6.4)."""
        async def burst_runner(transport: InMemoryTransport, spec: SessionSpec) -> None:
            for i in range(5):
                await transport.send({  # type: ignore[arg-type]
                    "v": 1, "type": "msg.chunk", "seq": i,
                    "ts": "2026-01-01T00:00:00Z",
                    "data": {"i": i},
                })
            await transport.send({  # type: ignore[arg-type]
                "v": 1, "type": "session.end", "seq": 5,
                "ts": "2026-01-01T00:00:00Z",
                "status": "completed",
            })

        exec_ = InProcessExecutor(runner=burst_runner)
        spec = SessionSpec(
            prompt="burst", cwd=tmp_path, plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )
        handle = await exec_.start(spec)
        await asyncio.sleep(0.01)
        frames = []
        from gg_relay.session.transport import TransportClosed
        try:
            while True:
                frames.append(await handle.transport.recv())
        except TransportClosed:
            pass
        assert len(frames) == 6
        assert frames[-1]["type"] == "session.end"
        await exec_.stop(handle)
```

> 测试侧前置依赖：`InMemoryTransport.recv()` 必须支持「peer 已关闭但 inbound 仍有数据」的 drain-then-close 语义（spec §6.4）。Task 3/4 提交的初版 transport 有 eager `_alive` 检查（commit `15828ce`/`be2bf8a`），Task 7 实施时显式去除（commit `25c4f65`），是 Task 7 的硬前置改动而非可有可无的优化。

- [ ] **Step 7.2: 跑测试确认失败**

```bash
pytest tests/unit/session/test_executor_inprocess.py -v
```

Expected: `ImportError`

- [ ] **Step 7.3: 实现 inprocess.py**

写入 `src/gg_relay/session/executor/inprocess.py`：

```python
"""InProcessExecutor — spawn runner coroutine in the same event loop.

The runner callable receives (runner_side_transport, spec) and is responsible
for driving the SDK (or stubbed equivalent). When the runner returns, the
runner-side transport is closed automatically.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime

from gg_relay.session.executor.protocol import RunnerFn
from gg_relay.session.spec import RuntimeHandle, SessionSpec
from gg_relay.session.transport.inmemory import make_pair

# Re-exported so existing `from gg_relay.session.executor.inprocess import RunnerFn`
# call sites keep working. Canonical definition lives in executor/protocol.py.
__all__ = ["InProcessExecutor", "RunnerFn"]


class InProcessExecutor:
    """Runs the runner callable as an asyncio task in the same event loop."""

    def __init__(self, runner: RunnerFn) -> None:
        self._runner = runner
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self, spec: SessionSpec) -> RuntimeHandle:
        host_side, runner_side = make_pair()
        runtime_id = uuid.uuid4().hex

        async def runner_wrapper() -> None:
            try:
                await self._runner(runner_side, spec)
            finally:
                await runner_side.close()

        task = asyncio.create_task(runner_wrapper(), name=f"runner-{runtime_id}")
        self._tasks[runtime_id] = task
        # Auto-drop entry on natural completion so _tasks doesn't grow unbounded
        # when SessionManager (Task 10+) drives sessions whose normal exit isn't
        # paired with a stop() call. stop() also pops; pop(default) is idempotent.
        task.add_done_callback(lambda _t: self._tasks.pop(runtime_id, None))

        return RuntimeHandle(
            backend="inprocess",
            runtime_id=runtime_id,
            transport=host_side,
            started_at=datetime.now(UTC),
        )

    async def stop(self, handle: RuntimeHandle) -> None:
        task = self._tasks.pop(handle.runtime_id, None)
        if task is not None and not task.done():
            task.cancel()
            # Swallow CancelledError + any runner exception; we are tearing
            # down. Runner failures surface to the host side via TransportClosed
            # on the next recv() (runner_wrapper closes the transport in finally).
            # NOT BaseException — SystemExit / KeyboardInterrupt must propagate.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await handle.transport.close()

    async def health(self, handle: RuntimeHandle) -> bool:
        return handle.transport.is_alive
```

> **模板与初稿的差异（已修正）**：
> 1. `RunnerFn` 用 `InMemoryTransport` 直接标注，移除 `InMemoryTransportLike` 别名 + 末位 `# noqa: E402` 后置导入（mypy strict + ruff 都会报）。
> 2. `datetime.now(timezone.utc)` → `datetime.now(UTC)`（仓库约定，ruff UP017）。
> 3. `dict[str, asyncio.Task]` → `dict[str, asyncio.Task[None]]`（mypy strict 需要泛型参数）。
> 4. `try/except (CancelledError, Exception): pass` → `contextlib.suppress(asyncio.CancelledError, Exception)`（ruff SIM105；用窄类型而非 `BaseException`，让 `SystemExit` / `KeyboardInterrupt` 正常向上传递）。
> 5. `transport=host_side,  # type: ignore[arg-type]` 中的 ignore 已删除（`SessionTransport` 是 `runtime_checkable` Protocol，结构子类型 mypy 接受）。
> 6. 测试侧 `_stub_runner` / `bad_runner` 加上 `transport: InMemoryTransport, spec: SessionSpec` 类型标注；`transport.send({...})` 加 `# type: ignore[arg-type]`（host-side Protocol 签名是 `ControlFrame`，runner-side 实际写的是 `EventFrame`，与 `test_transport_inmemory.py` 现有约定一致）。
> 7. `start()` 末尾 `task.add_done_callback(lambda _t: self._tasks.pop(runtime_id, None))`：runner 自然完成时自动清理 `_tasks`，防止 SessionManager 长跑时 dict 泄漏（quality review I-2）。
> 8. `RunnerFn` 加上协作式取消契约 docstring：runner 必须有 await point 否则 `stop()` 死等；同时提示 Task 8+ 真 SDK runner 要把异常先打成 `error` event frame 再 raise，让宿主能拿到根因而不只是 `TransportClosed`（quality review I-3）。

- [ ] **Step 7.4: 跑测试确认通过**

```bash
pytest tests/unit/session/test_executor_inprocess.py -v
```

Expected: 7 passed（3 base + 4 lifecycle/drain 回归）。

- [ ] **Step 7.5: 提交**

```bash
git add src/gg_relay/session/executor/inprocess.py tests/unit/session/test_executor_inprocess.py
git commit -m "feat(executor): InProcessExecutor with pluggable runner

- start() 用 asyncio.create_task 起 runner 协程
- stop() cancel + transport.close()
- runner 异常通过 transport.close() 让 host 侧 recv() 抛 TransportClosed"
```

---

## Task 8: 宿主侧协调器 `client.py` + 真 SDK runner

**Files:**
- Create: `src/gg_relay/session/client.py`
- Create: `tests/integration/test_walking_skeleton.py`

> 这一步把 4-7 的拼装件接到真的 `claude-code-sdk` 上。`client.py` 提供两个东西：
>   1. `make_sdk_runner(policy, coord)` — 返回一个 RunnerFn 给 InProcessExecutor 用
>   2. `GgRelayClaudeClient` — 宿主侧的高层 API（持 transport 消费事件流）

- [x] **Step 8.1: 写集成测试（标记 requires_sdk，不需要真 API key）**

更新 `pyproject.toml` 增加 marker（如果还没）。在 `[tool.pytest.ini_options]` 段：

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "--cov=gg_relay --cov-report=term-missing --cov-fail-under=80"
testpaths = ["tests"]
markers = [
    "requires_sdk: tests that exercise claude-code-sdk integration (no API call)",
    "requires_api_key: tests that hit the real Anthropic API (skipped without key)",
]
```

写入 `tests/integration/test_walking_skeleton.py`：

```python
"""End-to-end walking skeleton: SessionSpec → InProcessExecutor → SDK runner → events.

Uses a stub SDK transport (claude_code_sdk.Transport subclass) so no API call is made.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.requires_sdk


async def test_walking_skeleton_completes(tmp_path: Path) -> None:
    """handler → InProcessExecutor → stub-SDK runner → see msg.chunk + session.end."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.hitl.policy import DEFAULT_POLICY
    from gg_relay.session.spec import PluginManifest, SessionSpec

    coord = HITLCoordinator()
    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=coord,
        sdk_factory=_make_stub_sdk_client,
    )
    executor = InProcessExecutor(runner=runner)

    spec = SessionSpec(
        prompt="say hello",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
    )
    handle = await executor.start(spec)

    frames: list[dict[str, Any]] = []
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=1.0)
        except Exception:
            break
        frames.append(dict(f))
        if f["type"] == "session.end":
            break

    await executor.stop(handle)

    types = [f["type"] for f in frames]
    assert "msg.chunk" in types
    assert "session.end" in types
    assert frames[-1]["status"] == "completed"


async def test_walking_skeleton_auto_accept_write(tmp_path: Path) -> None:
    """When stub SDK requests a Write inside cwd, can_use_tool returns Allow."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.hitl.policy import DEFAULT_POLICY
    from gg_relay.session.spec import PluginManifest, SessionSpec

    target = tmp_path / "out.txt"
    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=lambda options: _StubWriteAttemptClient(options, file_path=str(target)),
    )
    executor = InProcessExecutor(runner=runner)
    spec = SessionSpec(
        prompt="write a file",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
    )
    handle = await executor.start(spec)

    decisions: list[bool] = []
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=1.0)
        except Exception:
            break
        if f["type"] == "tool.result":
            decisions.append(bool(f["ok"]))  # type: ignore[typeddict-item]
        if f["type"] == "session.end":
            break
    await executor.stop(handle)
    assert decisions == [True]   # the Write was allowed


async def test_walking_skeleton_hitl_path_blocks_then_approves(tmp_path: Path) -> None:
    """Bash request → policy says NEEDS_HITL → coord resolved externally → allowed."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.hitl.policy import DEFAULT_POLICY
    from gg_relay.session.spec import PluginManifest, SessionSpec

    coord = HITLCoordinator()

    async def auto_approve_after_delay() -> None:
        # wait until something is pending, then approve
        for _ in range(50):
            snap = coord.pending_snapshot()
            if snap:
                req_id = next(iter(snap))
                await coord.resolve(req_id, "accept")
                return
            await asyncio.sleep(0.02)

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=coord,
        sdk_factory=lambda options: _StubBashAttemptClient(options),
    )
    executor = InProcessExecutor(runner=runner)
    spec = SessionSpec(
        prompt="run ls",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
    )
    asyncio.create_task(auto_approve_after_delay())

    handle = await executor.start(spec)
    bash_result_ok = False
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=2.0)
        except Exception:
            break
        if f["type"] == "tool.result" and f.get("ok") is True:  # type: ignore[typeddict-item]
            bash_result_ok = True
        if f["type"] == "session.end":
            break
    await executor.stop(handle)
    assert bash_result_ok, "Bash tool should have been approved via HITL"


async def test_walking_skeleton_hitl_path_deny(tmp_path: Path) -> None:
    """coord.resolve(req_id, 'deny') → can_use_tool returns Deny → stub sees behavior == 'deny'."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.hitl.policy import DEFAULT_POLICY
    from gg_relay.session.spec import PluginManifest, SessionSpec

    coord = HITLCoordinator()

    async def auto_deny_after_delay() -> None:
        for _ in range(50):
            snap = coord.pending_snapshot()
            if snap:
                req_id = next(iter(snap))
                await coord.resolve(req_id, "deny", reason="not safe")
                return
            await asyncio.sleep(0.02)

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=coord,
        sdk_factory=lambda options: _StubBashAttemptClient(options),
    )
    executor = InProcessExecutor(runner=runner)
    spec = SessionSpec(
        prompt="run ls", cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"), executor="inprocess",
    )
    asyncio.create_task(auto_deny_after_delay())

    handle = await executor.start(spec)
    bash_result_ok: bool | None = None
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=2.0)
        except Exception:
            break
        if f["type"] == "tool.result":
            bash_result_ok = bool(f["ok"])  # type: ignore[typeddict-item]
        if f["type"] == "session.end":
            break
    await executor.stop(handle)
    assert bash_result_ok is False, "Bash tool should have been denied via HITL"


async def test_walking_skeleton_policy_deny(tmp_path: Path) -> None:
    """A custom ToolPolicy that returns Decision.DENY → can_use_tool returns Deny."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.spec import Decision, PluginManifest, SessionSpec

    class _AlwaysDenyPolicy:
        """Mock policy that returns DENY for Bash, ACCEPT for everything else."""

        def decide(self, tool: str, args: object, cwd: object) -> Decision:
            if tool == "Bash":
                return Decision.DENY
            return Decision.ACCEPT

    runner = make_sdk_runner(
        policy=_AlwaysDenyPolicy(),  # type: ignore[arg-type]
        coordinator=HITLCoordinator(),
        sdk_factory=lambda options: _StubBashAttemptClient(options),
    )
    executor = InProcessExecutor(runner=runner)
    spec = SessionSpec(
        prompt="ls", cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"), executor="inprocess",
    )
    handle = await executor.start(spec)
    bash_result_ok: bool | None = None
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=2.0)
        except Exception:
            break
        if f["type"] == "tool.result":
            bash_result_ok = bool(f["ok"])  # type: ignore[typeddict-item]
        if f["type"] == "session.end":
            break
    await executor.stop(handle)
    assert bash_result_ok is False, "Policy DENY should have produced PermissionResultDeny"


async def test_walking_skeleton_error_frame_on_runner_exception(tmp_path: Path) -> None:
    """Runner exception → error frame published before TransportClosed."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.hitl.policy import DEFAULT_POLICY
    from gg_relay.session.spec import PluginManifest, SessionSpec

    class _ExplodingClient(_StubBaseClient):
        async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
            if False:  # make this an async-generator without yielding
                yield {}
            raise RuntimeError("simulated SDK failure")

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=lambda options: _ExplodingClient(options),
    )
    executor = InProcessExecutor(runner=runner)
    spec = SessionSpec(
        prompt="x", cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"), executor="inprocess",
    )
    handle = await executor.start(spec)
    seen_error = False
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=1.0)
        except Exception:
            break
        if f["type"] == "error":
            seen_error = True
            assert f["code"] == "RuntimeError"  # type: ignore[typeddict-item]
            assert "simulated SDK failure" in f["message"]  # type: ignore[typeddict-item]
            break
    await executor.stop(handle)
    assert seen_error, "error frame must be emitted before close on runner exception"


async def test_walking_skeleton_factory_exception_publishes_error(tmp_path: Path) -> None:
    """sdk_factory itself raising → error frame published (I-1 regression)."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.hitl.policy import DEFAULT_POLICY
    from gg_relay.session.spec import PluginManifest, SessionSpec

    def _exploding_factory(_options: Any) -> Any:
        raise RuntimeError("factory boom")

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=_exploding_factory,
    )
    executor = InProcessExecutor(runner=runner)
    spec = SessionSpec(
        prompt="x", cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"), executor="inprocess",
    )
    handle = await executor.start(spec)
    seen_error = False
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=1.0)
        except Exception:
            break
        if f["type"] == "error":
            assert f["code"] == "RuntimeError"  # type: ignore[typeddict-item]
            assert "factory boom" in f["message"]  # type: ignore[typeddict-item]
            seen_error = True
            break
    await executor.stop(handle)
    assert seen_error, "factory exception must publish an error frame (I-1)"


async def test_walking_skeleton_cancellation_no_false_error(tmp_path: Path) -> None:
    """executor.stop() mid-flight must NOT publish error frame with code=CancelledError (I-2)."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.hitl.policy import DEFAULT_POLICY
    from gg_relay.session.spec import PluginManifest, SessionSpec

    class _NeverEndsClient(_StubBaseClient):
        async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
            while True:
                await asyncio.sleep(0.05)
                yield {"type": "AssistantMessage", "content": "still going"}

    runner = make_sdk_runner(
        policy=DEFAULT_POLICY,
        coordinator=HITLCoordinator(),
        sdk_factory=lambda options: _NeverEndsClient(options),
    )
    executor = InProcessExecutor(runner=runner)
    spec = SessionSpec(
        prompt="x", cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"), executor="inprocess",
    )
    handle = await executor.start(spec)

    # Drain at least one frame to ensure runner is actively yielding
    await asyncio.wait_for(handle.transport.recv(), timeout=1.0)
    # Now yank the runner
    await executor.stop(handle)

    # Drain remaining frames; assert no CancelledError-coded error frame
    seen_false_error = False
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=0.3)
        except Exception:
            break
        if f["type"] == "error" and f.get("code") == "CancelledError":  # type: ignore[typeddict-item]
            seen_false_error = True
            break
    assert not seen_false_error, "cancellation must not surface as an error frame (I-2)"


# ── Stub SDK clients (avoid hitting real API) ──────────────────────────────


class _StubBaseClient:
    """Common stub matching the subset of ClaudeSDKClient we use."""

    def __init__(self, options: Any) -> None:
        self._options = options

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def query(self, prompt: str) -> None: ...
    async def interrupt(self) -> None: ...


def _make_stub_sdk_client(options: Any) -> _StubBaseClient:
    """Minimal stub: just yields one assistant message and ends."""

    class _C(_StubBaseClient):
        async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "AssistantMessage", "content": "hi"}
            yield {"type": "ResultMessage", "subtype": "success", "total_cost_usd": 0.0}

    return _C(options)


class _StubWriteAttemptClient(_StubBaseClient):
    """Stub that triggers options.can_use_tool with a Write request."""

    def __init__(self, options: Any, file_path: str) -> None:
        super().__init__(options)
        self._file_path = file_path

    async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
        from claude_code_sdk import ToolPermissionContext
        ctx = ToolPermissionContext(signal=None, suggestions=[])
        result = await self._options.can_use_tool(
            "Write", {"file_path": self._file_path, "content": "x"}, ctx
        )
        ok = result.behavior == "allow"
        yield {
            "type": "ToolResult",
            "tool_name": "Write",
            "ok": ok,
            "result": {"file_path": self._file_path},
        }
        yield {"type": "ResultMessage", "subtype": "success", "total_cost_usd": 0.0}


class _StubBashAttemptClient(_StubBaseClient):
    async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
        from claude_code_sdk import ToolPermissionContext
        ctx = ToolPermissionContext(signal=None, suggestions=[])
        result = await self._options.can_use_tool(
            "Bash", {"command": "ls"}, ctx
        )
        ok = result.behavior == "allow"
        yield {
            "type": "ToolResult",
            "tool_name": "Bash",
            "ok": ok,
            "result": {"stdout": "."},
        }
        yield {"type": "ResultMessage", "subtype": "success", "total_cost_usd": 0.0}
```

- [x] **Step 8.2: 跑测试确认失败**

```bash
pytest tests/integration/test_walking_skeleton.py -v
```

Expected: `ImportError: cannot import name 'make_sdk_runner'`

- [x] **Step 8.3: 实现 client.py**

写入 `src/gg_relay/session/client.py`：

```python
"""GgRelayClaudeClient + make_sdk_runner.

The runner factory wires together:
  - ClaudeSDKClient (or stub) — owns the actual SDK conversation
  - ClaudeCodeOptions.can_use_tool — host-side ToolPolicy + HITLCoordinator
  - InMemoryTransport (runner side) — pipes SDK events as event frames to the host

The host side of the transport is consumed by the calling handler / SessionManager
(out of scope for this Plan; will be added in Plan 4).
"""
from __future__ import annotations

import asyncio
import contextlib
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from claude_code_sdk import (
    ClaudeCodeOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from gg_relay.session.executor.protocol import RunnerFn
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.spec import Decision, SessionSpec
from gg_relay.session.transport.inmemory import InMemoryTransport
from gg_relay.session.transport.protocol import EventFrame

SdkFactory = Callable[[ClaudeCodeOptions], Any]
"""Factory returning a ClaudeSDKClient-like object.

Returns Any (not ClaudeSDKClient) so test stubs can satisfy the duck-typed
surface (connect/disconnect/query/receive_messages) without inheriting from
the real SDK class.
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _envelope(seq: int, type_: str, **rest: Any) -> dict[str, Any]:
    """Build a wire-format frame dict for transport.send.

    Sequence numbering note: `seq` is **monotonic but not gapless**. The
    ResultMessage branch in the runner increments `seq` twice — once for the
    assistant message body itself, once for the trailing `session.end` frame —
    so consumers must not assume contiguous integer ranges. They should only
    rely on strict ordering (later frame ⇒ strictly larger `seq`).
    """
    return {"v": 1, "type": type_, "seq": seq, "ts": _now_iso(), **rest}


def make_sdk_runner(
    *,
    policy: ToolPolicy,
    coordinator: HITLCoordinator,
    sdk_factory: SdkFactory = ClaudeSDKClient,
) -> RunnerFn:
    """Return a RunnerFn suitable for InProcessExecutor(runner=...).

    **SCOPE — Plan 1, in-process only.** This factory takes the host's
    HITLCoordinator directly; it never consumes ControlFrames from the
    transport. For cross-process backends (Plan 3 Docker, future K8s),
    a separate ``make_wire_runner`` will route HITL decisions via
    ``tool.decision`` ControlFrames instead.

    The returned coroutine owns the lifecycle of one SDK conversation:
    connect → query → drain receive_messages → disconnect (in finally).
    Each SDK message becomes a transport EventFrame on the runner side, which
    propagates to the host via the paired InMemoryTransport.
    """

    async def runner(transport: InMemoryTransport, spec: SessionSpec) -> None:
        seq = 0

        async def can_use_tool(
            tool_name: str,
            tool_input: dict[str, Any],
            context: ToolPermissionContext,
        ) -> PermissionResultAllow | PermissionResultDeny:
            d = policy.decide(tool_name, tool_input, spec.cwd)
            if d == Decision.ACCEPT:
                return PermissionResultAllow()
            if d == Decision.DENY:
                return PermissionResultDeny(message=f"policy denied {tool_name}")
            # NEEDS_HITL → publish tool.request, await coordinator decision.
            # 12 hex chars = 48 bits, birthday-bound ~16M concurrent pending
            # (vs 8 hex / 32 bits → only ~65K before 50% collision).
            req_id = f"r-{uuid.uuid4().hex[:12]}"
            nonlocal seq
            seq += 1
            await transport.send(cast(EventFrame, _envelope(
                seq, "tool.request", req_id=req_id, tool=tool_name, args=tool_input,
            )))
            decision = await coordinator.request(req_id, tool=tool_name, args=tool_input)
            if decision == "accept":
                return PermissionResultAllow()
            return PermissionResultDeny(message="HITL rejected")

        options = ClaudeCodeOptions(
            can_use_tool=can_use_tool,
            cwd=str(spec.cwd),
            env=dict(spec.plugins.extra_env),
        )

        # sdk_factory() must be invoked INSIDE the try so a factory that raises
        # synchronously (bad options, ImportError, etc.) still surfaces as an
        # `error` event frame per RunnerFn contract (I-1). `client` is bound
        # to None first so the finally clause can guard against the
        # never-constructed case.
        client: Any = None
        try:
            client = sdk_factory(options)
            await client.connect()
            await client.query(spec.prompt)
            async for msg in client.receive_messages():
                seq += 1
                msg_type = msg.get("type") if isinstance(msg, dict) else type(msg).__name__
                if msg_type == "ToolResult" and isinstance(msg, dict):
                    await transport.send(cast(EventFrame, _envelope(
                        seq, "tool.result",
                        # TODO Plan 4: map SDK tool_use_id → host-side req_id; for now
                        # the dict-stub path passes req_id through verbatim or "".
                        req_id=msg.get("req_id", ""),
                        # Fail-safe default: a ToolResult without an "ok" field means
                        # we don't know if it succeeded, so treat it as failure.
                        ok=msg.get("ok", False),
                        result=msg.get("result", {}),
                    )))
                elif msg_type == "ResultMessage":
                    seq += 1
                    await transport.send(cast(EventFrame, _envelope(
                        seq, "session.end",
                        status="completed",
                        tokens=msg.get("usage", {}) if isinstance(msg, dict) else {},
                        cost_usd=(
                            msg.get("total_cost_usd", 0.0) if isinstance(msg, dict) else 0.0
                        ),
                    )))
                    break
                else:
                    await transport.send(cast(EventFrame, _envelope(
                        seq, "msg.chunk",
                        data=msg if isinstance(msg, dict) else {"repr": repr(msg)},
                    )))
        except asyncio.CancelledError:
            # Clean cancellation (e.g. executor.stop()) — propagate without
            # publishing a misleading `error` frame. runner_wrapper.finally
            # closes the transport so the host observes the session boundary
            # via TransportClosed. (I-2)
            raise
        except BaseException as exc:
            # Per RunnerFn contract (executor/inprocess.py docstring): runner
            # exceptions must surface to the host as an `error` frame before
            # propagating, otherwise the host only sees TransportClosed and
            # loses the root cause. Catch BaseException so KeyboardInterrupt /
            # SystemExit also publish the frame before unwinding. Suppress any
            # send failure so the original exception is re-raised intact.
            seq += 1
            with contextlib.suppress(Exception):
                await transport.send(cast(EventFrame, _envelope(
                    seq, "error",
                    code=type(exc).__name__,
                    message=str(exc),
                    traceback=traceback.format_exc(),
                )))
            raise
        finally:
            # Disconnect must not mask the original exception if it raises,
            # and must be skipped if sdk_factory() never produced a client.
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.disconnect()

    return runner
```

> **Plan-template fixes applied in Task 8 commit** (synced from implementation):
>
> 1. **`import json` removed** — runner doesn't serialize JSON; frames stay as dicts in-process.
> 2. **`datetime.timezone` → `datetime.UTC`** (ruff UP017, repo convention; matches `executor/inprocess.py` from Task 7).
> 3. **`cast(EventFrame, _envelope(...))` at each `transport.send` call** — `_envelope` returns `dict[str, Any]` but `InMemoryTransport.send` expects `ControlFrame | EventFrame` (TypedDict union). Cast is cleaner than per-call `# type: ignore[arg-type]`.
> 4. **Unused `from pathlib import Path`** removed.
> 5. **Test file**: typed `__init__`/methods on `_StubBaseClient` (Any, str, None returns); `AsyncIterator[dict[str, Any]]` return types on `receive_messages`; `dict(f)` coercion into `frames` list to satisfy `list[dict[str, Any]]`; `# type: ignore[typeddict-item]` on `f["ok"]` access (EventFrame union has no static `ok` key — only `ToolResultFrame` does, and TypedDict narrowing on `f["type"]` literal isn't picked up).
> 6. **`_StubBaseClient` empty methods use `...` (PEP 8) instead of `pass`** to keep ruff happy under `B`/`SIM`.
> 7. **`SdkFactory` docstring** added — explains why factory returns `Any` (duck-typing for test stubs vs. inheriting `ClaudeSDKClient`).
> 8. **Public `EventFrame` import** added to `client.py` to support the cast.
>
> `claude-code-sdk` ships `py.typed` so mypy strict resolves without an `[[tool.mypy.overrides]]` block.
>
> **Follow-up fixes (post-`db4a7fd` spec review)** — applied as a separate commit on top:
>
> A. **Issue [A] (MEDIUM): error event frame on runner exception**. The runner now catches `BaseException`, publishes an `error` envelope (`code=type(exc).__name__`, `message=str(exc)`, `traceback=traceback.format_exc()`) before re-raising. Honors the `RunnerFn` contract documented in `executor/inprocess.py` and means the host gets a meaningful root cause instead of a bare `TransportClosed`. Requires `import contextlib` and `import traceback` at the top of the module; the `finally: client.disconnect()` is also wrapped in `contextlib.suppress(Exception)` so a disconnect failure can't mask the original exception. `ErrorFrame` is already in the `EventFrame` union from Task 2, so no protocol change was needed.
>
> B. **Issue [C.1] (LOW): fail-safe `ok` default**. Changed `ok=msg.get("ok", True)` → `ok=msg.get("ok", False)`. Missing field = unknown outcome → fail-safe direction. Also added a `TODO Plan 4` comment on the `req_id=msg.get("req_id", "")` line — the SDK uses `tool_use_id` and the host needs to map it back to its own `req_id`; that mapping lives in SessionManager (Plan 4), not here.
>
> C. **Issue [F.1/F.2] (MEDIUM): deny-path tests + error-frame test**. Three new tests added to `tests/integration/test_walking_skeleton.py`:
>    - `test_walking_skeleton_hitl_path_deny` — `coord.resolve(req_id, "deny", reason=...)` → `can_use_tool` returns `PermissionResultDeny` → stub sees `behavior == "deny"`, yields `ok=False`, runner forwards `tool.result` with `ok=False`.
>    - `test_walking_skeleton_policy_deny` — Custom `_AlwaysDenyPolicy` returning `Decision.DENY` for Bash → `can_use_tool` short-circuits without involving the coordinator → same `ok=False` outcome.
>    - `test_walking_skeleton_error_frame_on_runner_exception` — `_ExplodingClient.receive_messages` raises `RuntimeError("simulated SDK failure")` → host reads an `error` frame with `code="RuntimeError"` and the message verbatim before `TransportClosed`. Brings `client.py` to **100%** coverage.
>
> **Explicitly deferred to Plan 4** (SessionManager + real SDK dispatch): dataclass message unwrapping (Issue [B.1-3]), `tool_use_id → req_id` mapping (Issue [B.3 + C.2]), concurrent multi-tool HITL test (Issue [F.4] — already covered by `HITLCoordinator` unit `test_concurrent_requests`), `tool.request` frame field assertions (Issue [F.5]). Cosmetic `req_id` length (Issue [G]) and `seq` gap polish (Issue [E]) are Task 10 work.
>
> **Error-frame boundary fixes (post-`333dd81` quality review)** — landed as a separate commit:
>
> D. **Issue [I-1] (IMPORTANT): factory exception bypasses error frame**. `sdk_factory(options)` was originally called *before* entering the try block, so a factory that raised synchronously (bad options, ImportError, etc.) escaped without publishing an `error` frame. Moved the call **inside** the try and pre-bound `client: Any = None` so the finally clause can `if client is not None` guard the `client.disconnect()`.
>
> E. **Issue [I-2] (IMPORTANT): `CancelledError` surfaced as misleading error frame**. `executor.stop()` cancels the runner task, which propagates `asyncio.CancelledError` into the runner body. The pre-existing `except BaseException` catch-all swallowed it and published `{"type": "error", "code": "CancelledError", ...}` — host code would mistake clean teardown for a runner crash. Added an `except asyncio.CancelledError: raise` clause **before** the `BaseException` catch; cancellation now propagates cleanly and `runner_wrapper.finally` still closes the transport so the host observes the session boundary via `TransportClosed`. Added `import asyncio` to the module.
>
> Two new TDD regression tests cover both fixes (RED-then-GREEN verified): `test_walking_skeleton_factory_exception_publishes_error` and `test_walking_skeleton_cancellation_no_false_error`. Together with prior commits this brings `client.py` and the rest of the `session` package to **100% coverage** (8 integration + 53 unit = 61 total tests).
>
> **Final-review polish (Plan 1 closeout)** — landed in a separate refactor commit:
>
> F. **Issue [I-2] (final review): make_sdk_runner docstring SCOPE stanza**. Added an explicit ``**SCOPE — Plan 1, in-process only.**`` lead-in clarifying that this factory reaches the host's ``HITLCoordinator`` directly and never consumes ``ControlFrame``s. Plan 3 (Docker) will introduce a separate ``make_wire_runner`` that routes HITL decisions via ``tool.decision`` ControlFrames over the wire.
>
> G. **Issue [I-3] (final review): RunnerFn deduped**. ``RunnerFn = Callable[[InMemoryTransport, SessionSpec], Awaitable[None]]`` was defined twice (``executor/inprocess.py`` and ``client.py``). Canonical home is now ``executor/protocol.py`` (it IS the executor's contract). Both call sites re-export the symbol so existing imports keep working. No cycle risk: ``executor/protocol.py → transport/inmemory → transport/protocol → stdlib``.

- [x] **Step 8.4: 跑集成测试**

```bash
pytest tests/integration/test_walking_skeleton.py -v
```

Expected: 3 passed.

- [x] **Step 8.5: 提交**

```bash
git add src/gg_relay/session/client.py tests/integration/test_walking_skeleton.py pyproject.toml
git commit -m "feat(session): GgRelayClaudeClient + make_sdk_runner — wires SDK to transport

- can_use_tool 绑定 ToolPolicy + HITLCoordinator
- SDK 消息流转 EventFrame 写入 InMemoryTransport
- sdk_factory 参数允许测试时注入 stub
- 3 个端到端测试覆盖：基本完成 / auto-accept Write / HITL Bash 路径

Spike § 验证：can_use_tool 是 async + PermissionResult 返回 — 全部直接落地，无降级。"
```

---

## Task 9: 端到端 demo 脚本

**Files:**
- Create: `examples/walking_skeleton_demo.py`

- [ ] **Step 9.1: 写 demo**

写入 `examples/walking_skeleton_demo.py`：

> **Implementation note (synced from Task 9 commit):** the original template
> had several mypy-strict / ruff violations. The version below is the final
> shipped code. Key fixes:
>
> 1. `_DemoSDK.__init__` parameter typed as `options: Any` (mypy strict refuses
>    untyped params); also `-> None` on all stub coroutines.
> 2. `receive_messages` typed as `AsyncIterator[dict[str, Any]]`.
> 3. `except (asyncio.TimeoutError, Exception):` — `asyncio.TimeoutError` IS
>    `TimeoutError` (3.11+) IS an `Exception` subclass, so the tuple is
>    misleading. Split into explicit `except TimeoutError` and
>    `except TransportClosed` branches so each path can print a useful reason
>    (timeout vs runner shutdown).
> 4. Hold a named reference to the `asyncio.create_task(im_responder(...))`
>    return value (responder_task) and cancel + `contextlib.suppress` it in
>    `finally` — fire-and-forget tasks can be GC'd mid-flight under load and
>    swallow exceptions.

```python
"""Walking-skeleton end-to-end demo.

Run:
  source .venv/bin/activate
  python examples/walking_skeleton_demo.py

This uses a stub SDK (no real API call). It demonstrates:
  1. handler builds SessionSpec
  2. InProcessExecutor starts runner
  3. host consumes EventFrames from transport
  4. NEEDS_HITL frame is auto-approved by a side-task (simulating an IM responder)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from gg_relay.session.client import make_sdk_runner
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import DEFAULT_POLICY
from gg_relay.session.spec import PluginManifest, SessionSpec
from gg_relay.session.transport.protocol import TransportClosed


class _DemoSDK:
    """Fake SDK that requests Write (auto-accept) then Bash (HITL) then ends.

    Duck-typed against ClaudeSDKClient; consumed via make_sdk_runner's
    `sdk_factory` hook so the demo runs without a real API key.
    `options` is typed as Any because the runner only reads `.can_use_tool`
    and `.cwd`, and constraining to ClaudeCodeOptions here adds no safety.
    """

    def __init__(self, options: Any) -> None:
        self._options = options

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        return None

    async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
        from claude_code_sdk import ToolPermissionContext

        ctx = ToolPermissionContext(signal=None, suggestions=[])

        # Write under cwd → policy ACCEPT → no HITL round-trip
        write_result = await self._options.can_use_tool(
            "Write",
            {
                "file_path": str(Path(self._options.cwd) / "demo.txt"),
                "content": "hello",
            },
            ctx,
        )
        yield {
            "type": "ToolResult",
            "tool_name": "Write",
            "ok": write_result.behavior == "allow",
        }

        # Bash → policy NEEDS_HITL → runner publishes tool.request, awaits coordinator
        bash_result = await self._options.can_use_tool(
            "Bash",
            {"command": "ls /tmp"},
            ctx,
        )
        yield {
            "type": "ToolResult",
            "tool_name": "Bash",
            "ok": bash_result.behavior == "allow",
        }

        yield {"type": "ResultMessage", "subtype": "success", "total_cost_usd": 0.0}


async def im_responder(coord: HITLCoordinator) -> None:
    """Simulate an IM user approving HITL requests as soon as they appear."""
    for _ in range(100):
        snap = coord.pending_snapshot()
        if snap:
            for req_id, info in snap.items():
                print(f"  [IM] approving {req_id}: tool={info['tool']} args={info['args']}")
                await coord.resolve(req_id, "accept")
            return
        await asyncio.sleep(0.05)


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        coord = HITLCoordinator()
        runner = make_sdk_runner(
            policy=DEFAULT_POLICY,
            coordinator=coord,
            sdk_factory=_DemoSDK,
        )
        executor = InProcessExecutor(runner=runner)

        spec = SessionSpec(
            prompt="demo prompt",
            cwd=cwd,
            plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )

        print(f"▶ Starting session in {cwd}")
        # Keep a reference so the responder isn't garbage-collected mid-flight.
        responder_task = asyncio.create_task(im_responder(coord))
        try:
            handle = await executor.start(spec)
            print(f"▶ runtime_id={handle.runtime_id}\n")

            while True:
                try:
                    frame = await asyncio.wait_for(handle.transport.recv(), timeout=3.0)
                except TimeoutError:
                    print("◀ timeout waiting for frame; aborting")
                    break
                except TransportClosed:
                    print("◀ transport closed by runner")
                    break
                print(f"◀ frame: {json.dumps(frame, default=str)[:200]}")
                if frame["type"] == "session.end":
                    break

            await executor.stop(handle)
        finally:
            responder_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await responder_task

        print("\n✓ session ended cleanly")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 9.2: 跑 demo**

```bash
source .venv/bin/activate
python examples/walking_skeleton_demo.py
```

Expected: 输出包含 "Starting session" → 一个 tool.request → "[IM] approving ..." →
两个 tool.result → 一个 session.end → "session ended cleanly"。

- [ ] **Step 9.3: 跑全部测试看通过**

```bash
pytest tests/ -v
```

Expected: 30+ tests passed, 0 failed.

- [ ] **Step 9.4: 提交**

```bash
git add examples/walking_skeleton_demo.py
git commit -m "demo(skeleton): walking-skeleton end-to-end with auto-approve IM responder

跑法: python examples/walking_skeleton_demo.py
演示 auto-accept Write + HITL Bash 完整链路，无需真 API key。"
```

---

## Task 10: 最终覆盖率检查与文档更新

- [ ] **Step 10.1: 跑覆盖率**

```bash
pytest tests/ --cov=gg_relay.session --cov-report=term-missing
```

Expected: `session.*` 模块覆盖率 ≥ 80%（PLAN.md P1-8 目标）。
如果有未覆盖代码，**针对性补充测试**（不要修改阈值）。

- [ ] **Step 10.2: 更新 README.md 增加 walking-skeleton 章节**

在 README.md 末尾追加（如果文件存在；如不存在按 PLAN.md 风格创建最小版）：

```markdown
## Quick Start: Walking Skeleton (in-process)

```bash
source .venv/bin/activate
python examples/walking_skeleton_demo.py
```

See `docs/superpowers/plans/2026-05-22-walking-skeleton-inprocess.md` for the
plan that built this; `docs/superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md`
for the design.
```

- [ ] **Step 10.3: 提交收尾**

```bash
git add README.md
git commit -m "docs: walking-skeleton quick start in README

完成 Plan 1：In-process backend with real-SDK wiring 全 9 个 task。
后续 Plan 2/3/4 在 docs/superpowers/plans/ 增量补。"
```

---

## Self-Review（按 writing-plans skill 要求）

### 1. Spec 覆盖检查

| Spec 节 | 本 Plan 覆盖 Task |
|---|---|
| §4.1 SessionSpec | Task 1 ✓ |
| §4.2 PluginManifest + to_install_argv | Task 1 ✓ |
| §4.3 ToolPolicy + Decision | Task 4 ✓ |
| §4.4 RuntimeHandle | Task 1 ✓ |
| §5.1 ExecutorBackend Protocol | Task 6 ✓ |
| §5.2 SessionTransport Protocol | Task 2 ✓ |
| §5.3 PluginAssembler Protocol | ✗ → **Plan 3 范围** |
| §6.2 帧格式 | Task 2 ✓ |
| §7.1 HITL 时序 | Task 8 + Task 9 demo ✓ |
| §8 基础镜像 | ✗ → **Plan 3 范围** |
| §9 PLAN.md 修订 | ✗ → **Plan 4 范围** |

**Spec 未覆盖项均已在 Plan 范围说明中明确为后续 plan**，无遗漏。

### 2. Placeholder 扫描

- 全部步骤包含完整可运行代码 ✓
- 无 "TBD" / "TODO" / "fill in details" ✓
- 测试代码完整可运行 ✓

### 3. 类型一致性

- `Decision` 枚举值 `accept|deny|needs_hitl` 在 spec.py 定义后被 policy.py、coordinator.py、
  client.py 一致引用 ✓
- `RuntimeHandle.transport: SessionTransport` 类型在 spec.py 用 TYPE_CHECKING 前置声明，避免循环导入 ✓
- `HITLCoordinator.resolve(decision: Literal["accept", "deny"])` 与 `request` 返回值类型对齐 ✓
- `ClaudeCodeOptions(can_use_tool=...)` 签名与 spike 报告确认的 SDK 接口一致 ✓

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-22-walking-skeleton-inprocess.md`.**
**Two execution options:**

1. **Subagent-Driven (推荐)** — 每个 Task 派一个新 subagent 跑，两阶段 review，快迭代
2. **Inline Execution** — 在当前会话按 executing-plans skill 批量执行，带 checkpoints

**Which approach?**
