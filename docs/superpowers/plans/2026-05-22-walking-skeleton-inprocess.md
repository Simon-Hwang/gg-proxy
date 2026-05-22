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
        inbound: asyncio.Queue,
        outbound: asyncio.Queue,
        paired: "InMemoryTransport | None" = None,
    ) -> None:
        self._inbound = inbound
        self._outbound = outbound
        self._paired = paired
        self._alive = True

    @property
    def is_alive(self) -> bool:
        return self._alive

    async def send(self, frame: ControlFrame | EventFrame) -> None:  # type: ignore[override]
        if not self._alive:
            raise TransportClosed("transport closed")
        await self._outbound.put(frame)

    async def recv(self) -> EventFrame:  # type: ignore[override]
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
    q_h2r: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    q_r2h: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
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

    def test_missing_path_needs_hitl(self):
        d = DEFAULT_POLICY.decide("Write", {}, Path("/work"))
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
            dangerous_patterns=(),
        )
        # Bash + no path-check (since no file_path key) → ACCEPT
        assert custom.decide("Bash", {"command": "ls"}, Path("/work")) == Decision.ACCEPT
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

HITL 工具（Bash/WebFetch/Task）→ 始终 NEEDS_HITL
中立工具（Read/Glob/Grep/LS）→ 始终 ACCEPT
未知工具 → NEEDS_HITL（保守）
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
_DEFAULT_DANGEROUS = (
    "*.env",
    "*/.git/*",
    "*/secrets/*",
    "*/credentials/*",
    "*id_rsa*",
    "*.pem",
)

_PATH_FIELDS = ("file_path", "notebook_path", "path")


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    auto_accept_tools: frozenset[str] = field(default=_DEFAULT_AUTO_ACCEPT)
    hitl_tools: frozenset[str] = field(default=_DEFAULT_HITL)
    neutral_tools: frozenset[str] = field(default=_DEFAULT_NEUTRAL)
    dangerous_patterns: tuple[str, ...] = field(default=_DEFAULT_DANGEROUS)

    def decide(self, tool: str, args: dict[str, Any], cwd: Path) -> Decision:
        if tool in self.neutral_tools:
            return Decision.ACCEPT
        if tool in self.auto_accept_tools:
            path = self._extract_path(args)
            if path is None:
                return Decision.NEEDS_HITL
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
        s = str(path)
        return any(fnmatch(s, pat) for pat in self.dangerous_patterns)


DEFAULT_POLICY = ToolPolicy()
```

- [ ] **Step 4.4: 跑测试确认通过**

```bash
pytest tests/unit/session/test_policy.py -v
```

Expected: 14 passed.

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

    async def test_double_resolve_idempotent(self):
        coord = HITLCoordinator()
        asyncio.create_task(coord.resolve("req-3", "accept"))
        await coord.request("req-3", tool="Bash", args={})
        # second resolve should not raise
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
from typing import Any, Literal


class HITLNotPending(LookupError):
    """resolve() called for a req_id not currently pending."""


@dataclass(frozen=True, slots=True)
class _PendingEntry:
    tool: str
    args: dict[str, Any]
    future: "asyncio.Future[tuple[str, str | None]]"


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
        return decision  # type: ignore[return-value]

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
        """Return a snapshot of all currently-pending requests."""
        return {
            rid: {"tool": e.tool, "args": e.args}
            for rid, e in self._pending.items()
            if not e.future.done()
        }
```

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
from gg_relay.session.executor.protocol import ExecutorBackend

__all__ = ["ExecutorBackend"]
```

写入 `src/gg_relay/session/executor/protocol.py`：

```python
"""ExecutorBackend Protocol — abstracts in-process vs. docker vs. (future) k8s."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from gg_relay.session.spec import RuntimeHandle, SessionSpec


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


async def _stub_runner(transport, spec) -> None:
    """A stub runner that just echoes one msg.chunk and ends."""
    await transport.send({
        "v": 1, "type": "msg.chunk", "seq": 0, "ts": "2026-01-01T00:00:00Z",
        "data": {"prompt": spec.prompt},
    })
    await transport.send({
        "v": 1, "type": "session.end", "seq": 1, "ts": "2026-01-01T00:00:00Z",
        "status": "completed",
    })


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
        # drain
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
        async def bad_runner(transport, spec):
            raise RuntimeError("boom")

        exec_ = InProcessExecutor(runner=bad_runner)
        spec = SessionSpec(
            prompt="hi", cwd=tmp_path, plugins=PluginManifest(profile="minimal"),
            executor="inprocess",
        )
        handle = await exec_.start(spec)
        # transport should still be valid; the exception is observed via stop()
        # or by recv() seeing TransportClosed when the runner finishes.
        from gg_relay.session.transport import TransportClosed
        with pytest.raises(TransportClosed):
            await handle.transport.recv()
        await exec_.stop(handle)
```

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
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from gg_relay.session.spec import RuntimeHandle, SessionSpec
from gg_relay.session.transport.inmemory import make_pair


RunnerFn = Callable[
    ["InMemoryTransportLike", SessionSpec],  # forward-ref via str alias
    Awaitable[None],
]

# Type alias only used in annotation
from gg_relay.session.transport.inmemory import InMemoryTransport as InMemoryTransportLike  # noqa: E402


class InProcessExecutor:
    """Runs the runner callable as an asyncio task in the same event loop."""

    def __init__(self, runner: RunnerFn) -> None:
        self._runner = runner
        self._tasks: dict[str, asyncio.Task] = {}

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

        return RuntimeHandle(
            backend="inprocess",
            runtime_id=runtime_id,
            transport=host_side,  # type: ignore[arg-type]
            started_at=datetime.now(timezone.utc),
        )

    async def stop(self, handle: RuntimeHandle) -> None:
        task = self._tasks.pop(handle.runtime_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await handle.transport.close()

    async def health(self, handle: RuntimeHandle) -> bool:
        return handle.transport.is_alive
```

- [ ] **Step 7.4: 跑测试确认通过**

```bash
pytest tests/unit/session/test_executor_inprocess.py -v
```

Expected: 3 passed.

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

- [ ] **Step 8.1: 写集成测试（标记 requires_sdk，不需要真 API key）**

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
from pathlib import Path

import pytest

pytestmark = pytest.mark.requires_sdk


async def test_walking_skeleton_completes(tmp_path: Path):
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

    frames = []
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=1.0)
        except Exception:
            break
        frames.append(f)
        if f["type"] == "session.end":
            break

    await executor.stop(handle)

    types = [f["type"] for f in frames]
    assert "msg.chunk" in types
    assert "session.end" in types
    assert frames[-1]["status"] == "completed"


async def test_walking_skeleton_auto_accept_write(tmp_path: Path):
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

    decisions = []
    while True:
        try:
            f = await asyncio.wait_for(handle.transport.recv(), timeout=1.0)
        except Exception:
            break
        if f["type"] == "tool.result":
            decisions.append(f["ok"])
        if f["type"] == "session.end":
            break
    await executor.stop(handle)
    assert decisions == [True]   # the Write was allowed


async def test_walking_skeleton_hitl_path_blocks_then_approves(tmp_path: Path):
    """Bash request → policy says NEEDS_HITL → coord resolved externally → allowed."""
    from gg_relay.session.client import make_sdk_runner
    from gg_relay.session.executor.inprocess import InProcessExecutor
    from gg_relay.session.hitl.coordinator import HITLCoordinator
    from gg_relay.session.hitl.policy import DEFAULT_POLICY
    from gg_relay.session.spec import PluginManifest, SessionSpec

    coord = HITLCoordinator()

    async def auto_approve_after_delay():
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
        if f["type"] == "tool.result" and f.get("ok") is True:
            bash_result_ok = True
        if f["type"] == "session.end":
            break
    await executor.stop(handle)
    assert bash_result_ok, "Bash tool should have been approved via HITL"


# ── Stub SDK clients (avoid hitting real API) ──────────────────────────────


class _StubBaseClient:
    """Common stub matching the subset of ClaudeSDKClient we use."""
    def __init__(self, options):
        self._options = options

    async def connect(self): pass
    async def disconnect(self): pass
    async def query(self, prompt: str): pass
    async def interrupt(self): pass


def _make_stub_sdk_client(options):
    """Minimal stub: just yields one assistant message and ends."""
    class _C(_StubBaseClient):
        async def receive_messages(self):
            yield {"type": "AssistantMessage", "content": "hi"}
            yield {"type": "ResultMessage", "subtype": "success", "total_cost_usd": 0.0}
    return _C(options)


class _StubWriteAttemptClient(_StubBaseClient):
    """Stub that triggers options.can_use_tool with a Write request."""
    def __init__(self, options, file_path: str):
        super().__init__(options)
        self._file_path = file_path

    async def receive_messages(self):
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
    async def receive_messages(self):
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

- [ ] **Step 8.2: 跑测试确认失败**

```bash
pytest tests/integration/test_walking_skeleton.py -v
```

Expected: `ImportError: cannot import name 'make_sdk_runner'`

- [ ] **Step 8.3: 实现 client.py**

写入 `src/gg_relay/session/client.py`：

```python
"""GgRelayClaudeClient + make_sdk_runner.

The runner factory wires together:
  - ClaudeSDKClient (or stub) — owns the actual SDK conversation
  - ClaudeCodeOptions.can_use_tool — host-side ToolPolicy + HITLCoordinator
  - InMemoryTransport (runner side) — pipes SDK events as JSONL frames to the host

The host side of the transport is consumed by the calling handler / SessionManager
(out of scope for this Plan; will be added in Plan 4).
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_code_sdk import (
    ClaudeCodeOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.spec import Decision, SessionSpec
from gg_relay.session.transport.inmemory import InMemoryTransport


SdkFactory = Callable[[ClaudeCodeOptions], Any]   # returns a ClaudeSDKClient-like object
RunnerFn = Callable[[InMemoryTransport, SessionSpec], Awaitable[None]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _envelope(seq: int, type_: str, **rest: Any) -> dict[str, Any]:
    return {"v": 1, "type": type_, "seq": seq, "ts": _now_iso(), **rest}


def make_sdk_runner(
    *,
    policy: ToolPolicy,
    coordinator: HITLCoordinator,
    sdk_factory: SdkFactory = ClaudeSDKClient,   # default: real SDK
) -> RunnerFn:
    """Return a RunnerFn suitable for InProcessExecutor(runner=...)."""

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
            # NEEDS_HITL → publish tool.request, await coordinator decision
            req_id = f"r-{uuid.uuid4().hex[:8]}"
            nonlocal seq
            seq += 1
            await transport.send(_envelope(
                seq, "tool.request", req_id=req_id, tool=tool_name, args=tool_input,
            ))
            decision = await coordinator.request(req_id, tool=tool_name, args=tool_input)
            if decision == "accept":
                return PermissionResultAllow()
            return PermissionResultDeny(message="HITL rejected")

        options = ClaudeCodeOptions(
            can_use_tool=can_use_tool,
            cwd=str(spec.cwd),
            env=dict(spec.plugins.extra_env),
        )

        client = sdk_factory(options)
        try:
            await client.connect()
            await client.query(spec.prompt)
            async for msg in client.receive_messages():
                seq += 1
                msg_type = msg.get("type") if isinstance(msg, dict) else type(msg).__name__
                if msg_type == "ToolResult" and isinstance(msg, dict):
                    await transport.send(_envelope(
                        seq, "tool.result",
                        req_id=msg.get("req_id", ""),
                        ok=msg.get("ok", True),
                        result=msg.get("result", {}),
                    ))
                elif msg_type == "ResultMessage":
                    seq += 1
                    await transport.send(_envelope(
                        seq, "session.end",
                        status="completed",
                        tokens=msg.get("usage", {}) if isinstance(msg, dict) else {},
                        cost_usd=msg.get("total_cost_usd", 0.0) if isinstance(msg, dict) else 0.0,
                    ))
                    break
                else:
                    await transport.send(_envelope(
                        seq, "msg.chunk",
                        data=msg if isinstance(msg, dict) else {"repr": repr(msg)},
                    ))
        finally:
            await client.disconnect()

    return runner
```

- [ ] **Step 8.4: 跑集成测试**

```bash
pytest tests/integration/test_walking_skeleton.py -v
```

Expected: 3 passed.

- [ ] **Step 8.5: 提交**

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
import json
import tempfile
from pathlib import Path

from gg_relay.session.client import make_sdk_runner
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import DEFAULT_POLICY
from gg_relay.session.spec import PluginManifest, SessionSpec


class _DemoSDK:
    """Fake SDK that requests Write (auto-accept) then Bash (HITL) then ends."""
    def __init__(self, options):
        self._options = options

    async def connect(self): pass
    async def disconnect(self): pass
    async def query(self, prompt: str): pass

    async def receive_messages(self):
        from claude_code_sdk import ToolPermissionContext
        ctx = ToolPermissionContext(signal=None, suggestions=[])

        # 1. Write (should auto-accept)
        write_result = await self._options.can_use_tool(
            "Write",
            {"file_path": str(Path(self._options.cwd) / "demo.txt"), "content": "hello"},
            ctx,
        )
        yield {"type": "ToolResult", "tool_name": "Write", "ok": write_result.behavior == "allow"}

        # 2. Bash (should HITL)
        bash_result = await self._options.can_use_tool(
            "Bash", {"command": "ls /tmp"}, ctx,
        )
        yield {"type": "ToolResult", "tool_name": "Bash", "ok": bash_result.behavior == "allow"}

        yield {"type": "ResultMessage", "subtype": "success", "total_cost_usd": 0.0}


async def im_responder(coord: HITLCoordinator) -> None:
    """Simulate an IM user approving HITL requests after 100ms."""
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
        asyncio.create_task(im_responder(coord))
        handle = await executor.start(spec)

        print(f"▶ runtime_id={handle.runtime_id}\n")
        while True:
            try:
                frame = await asyncio.wait_for(handle.transport.recv(), timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                break
            print(f"◀ frame: {json.dumps(frame, default=str)[:200]}")
            if frame["type"] == "session.end":
                break

        await executor.stop(handle)
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
