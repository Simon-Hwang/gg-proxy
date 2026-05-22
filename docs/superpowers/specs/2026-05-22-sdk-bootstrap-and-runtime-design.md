# SDK Bootstrap & Container Runtime Design

*Spec · 2026-05-22 · 基于 PLAN.md 的增量设计 · brainstorming 5 轮对齐*

---

## 1. 目标与范围

### 1.1 一句话目标

为 `gg-relay` 引入 `claude-code-sdk` 的执行能力，使其能被外部 handler 以
**结构化入参（声明所需 gg-plugins 资源）** 调用，**默认在隔离容器内执行**，
并通过 **长连接事件流 + Hook 桥接** 实现实时状态感知、文件类操作自动 accept、
其它操作走 HITL 人工审批（可后续接入 IM）。

### 1.2 范围

- 新增 `session/` 下 4 个子包：`executor/`、`assembly/`、`transport/`、`runner/`、`hitl/`
- 新增容器内可执行入口 `gg-relay-runner`（`python -m gg_relay.runner`）
- 新增基础镜像 `deploy/docker/runner.Dockerfile`
- 修订 PLAN.md §3、§5、§6、§8、§15 的少量条目（在第 9 节列出）
- **不变** PLAN.md 的 6 条架构不变量（EventBus 唯一扇出、Protocol 接口、SQLAlchemy Core、
  P0 安全、`ClaudeSDKClient` 唯一 SDK 接口、事件分级投递）

### 1.3 非目标（v1）

- 不实现 IM 卡片细节（沿用 PLAN.md §11 的 IMSubscriber 即可）
- 不实现 dashboard 上 HITL 控件（沿用 PLAN.md §11 HITL 反向 REST 端点）
- 不实现 cluster / 多 worker 分布（PLAN.md P6）
- 不实现 per-session 切 gg-plugins 版本（决策 D2-a：单版本）
- v1 仅实现 `DockerExecutor`，**不实现 `K8sExecutor`**

### 1.4 显式预留的扩展点（v1.x+）

下列扩展不在 v1 实现范围，但 Protocol 设计必须保证「不需要改 v1 既有代码」即可被第三方实现接入：

| 扩展 | 解决方案 |
|---|---|
| **k8s 后端**（替换 docker run 为 Pod 编排） | `ExecutorBackend` Protocol；新增 `K8sExecutor` 实现 |
| **跨 Pod transport**（Unix socket 不能跨 Pod） | `SessionTransport` Protocol；新增 `TcpSocketTransport` / `WebSocketTransport` |
| **远程 Docker daemon** | `DockerExecutor` 用 `DOCKER_HOST` 即可，无需新实现 |
| **第三方 IM/Slack 卡片样式** | 复用 PLAN.md §11 IMBackend Protocol |

**对 v1 实现的约束**：以下三处设计**必须**显式抽象，不允许出现 docker / unix socket 字面假设：
- `ExecutorBackend` Protocol 的方法签名（不出现 `container_id`、`docker_*` 字样；用 `runtime_id`）
- `SessionTransport` Protocol（不假设 socket 类型，只约定双向 JSONL 流语义）
- `HITLCoordinator` 与 transport 的交互（基于 `req_id` 路由，不依赖底层连接特性）

---

## 2. 已对齐的决策清单

| # | 决策点 | 选择 | 备注 |
|---|---|---|---|
| 1 | 执行后端架构 | **ExecutorBackend Protocol** + InProcess / Docker 两实现，默认 Docker | dev/test 用 in-process |
| 2 | 容器隔离粒度 | **一 session 一容器**（用完即销） | 启动 1-3s 可接受 |
| 3 | gg-plugins 装配方式 | **容器内调用 `gg-plugins/install.sh`**（仓库 ro-mount + 装到 `/root/.claude/`） | manifest 与 install.sh CLI 1:1 对齐 |
| 4 | Hook 层次 | **双层**：宿主侧 SDK hook 做 HITL 决策；容器内 plugins hooks.json 做业务检查 | |
| 5 | HITL 策略 | **工具类别 + 路径抽检**：文件类工具 + 路径在 cwd 子树 → 自动 accept；越界/黑名单/Bash/WebFetch/Task → HITL | |
| 6 | 宿主↔容器通信 | **长连接 JSON-Lines over Unix domain socket**（双向） | 取代批处理风格 RPC；HITL 同链路回灌 |
| 7 | HITL 暂停机制 | **PreToolUse 内同步阻塞** 等 socket 决策；不依赖 SDK `interrupt/resume` | SDK 不支持时降级为 message stream 截流 |
| 8 | node_modules 处理 | **multi-stage Docker build 烘进镜像**；运行时容器禁网 | D1-i |
| 9 | gg-plugins 多版本 | **单版本**：镜像 tag = gg-plugins commit sha | D2-a；不支持 per-session 切版本 |
| 10 | 镜像发布节奏 | gg-plugins **release tag 触发 CI build** → push `gg-relay-runner:<tag>` | D3-i |

---

## 3. 系统架构（增量视图）

```
┌────────────────────────────────────────────────────────────────────────────┐
│                              gg-relay 宿主进程                              │
│                                                                            │
│  handler ──SessionSpec──▶ SessionManager ──▶ ExecutorBackend.start(spec)   │
│                                                       │                    │
│                                          ┌────────────┴────────────┐       │
│                                          │ DockerExecutor          │       │
│                                          │  docker run -d \        │       │
│                                          │   -v gg-plugins:ro  \   │       │
│                                          │   -v cwd:/work      \   │       │
│                                          │   -v sock:/run/relay.sock        │
│                                          │   gg-relay-runner:tag   │       │
│                                          └────────────┬────────────┘       │
│                                                       │                    │
│                                          UnixSocketTransport (JSONL)       │
│                                                       │                    │
│  ┌──────────────┐                            ┌────────▼─────────┐         │
│  │ ToolPolicy   │◀────tool.request────────── │ GgRelayClaudeClnt │         │
│  │ HITLCoord    │─────tool.decision────────▶ │ (协调器，宿主侧) │         │
│  └──────┬───────┘                            └────────┬──────────┘         │
│         │                                             │                    │
│         │ publish RelayEvent                          │ publish RelayEvent │
│         ▼                                             ▼                    │
│         EventBus  ──▶  OTel / IM / SSE / Store                             │
│                            │                                               │
│                            └─ IMSubscriber 发飞书卡片 ──▶ 用户点击         │
│                            POST /api/v1/hitl/{id}/approve                  │
│                            ──▶ HITLCoord.wake(req_id) ──▶ 决策回灌 socket  │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
                                       │
                       Unix domain socket (双向 JSONL)
                                       │
┌──────────────────────────────────────▼───────────────────────────────────┐
│                container: gg-relay-runner:<gg-plugins-sha>               │
│                                                                          │
│  python -m gg_relay.runner --socket /run/relay.sock                      │
│   1. bash /opt/gg-plugins/install.sh <argv-from-manifest> --home /root   │
│      └─ 装到 /root/.claude/ (plugins/skills/agents/hooks/rules)          │
│   2. ClaudeSDKClient(cwd="/work")                                        │
│   3. on PreToolUse(tool, args):                                          │
│        req_id = uuid()                                                   │
│        socket.write({type:"tool.request", req_id, tool, args})           │
│        d = socket.read_until(type:"tool.decision", req_id)               │
│        return d.accept                                                   │
│   4. forward 所有 SDK 消息 → socket                                       │
│                                                                          │
│  ~/.claude/hooks/*.json 内 gg-plugins 自带的业务 hooks（lint/security）  │
└──────────────────────────────────────────────────────────────────────────┘
```

**关键性质**：

1. SDK 唯一改动点仍是 `session/client.py`（PLAN.md §14 SDK 契约）
2. 长连接断 = session 失败（zero-tolerance），由 ExecutorBackend.stop 兜底销毁容器
3. EventBus 仍是唯一扇出机制（PLAN.md §3 不变量 #1）

---

## 4. 核心数据模型

### 4.1 `SessionSpec` — handler 调用入参

```python
@dataclass(frozen=True, slots=True)
class SessionSpec:
    prompt:       str
    cwd:          Path
    plugins:      PluginManifest
    executor:     Literal["docker", "inprocess"] = "docker"
    timeout_s:    int = 1800
    metadata:     tuple[tuple[str, Any], ...] = ()
```

> **Plan 1 deviation:** the `hitl_policy: ToolPolicy | None` field originally
> proposed in this section is deferred to Plan 4 (SessionManager). Plan 1
> binds the policy at `make_sdk_runner` construction time, which is
> sufficient for the in-process case where a single policy serves all
> sessions in the same process. Per-session override will be reintroduced
> when SessionManager needs to differentiate (e.g. different tenants).

### 4.2 `PluginManifest` — 与 install.sh CLI 1:1 对齐

```python
@dataclass(frozen=True, slots=True)
class PluginManifest:
    """与 gg-plugins/install.sh CLI 严格对齐。

    三种装配模式至少选一种（可叠加，install.sh 自行合并）：
      - profile:  5 个预设之一（minimal/core/go/python/full）
      - modules:  直接列举 module ID（如 "rules-python", "skills-security"）
      - skills:   按 skill 目录名挑选个别 skill
    """
    profile: Literal["minimal", "core", "go", "python", "full"] | None = None
    modules: tuple[str, ...] = ()
    skills:  tuple[str, ...] = ()

    with_components:    tuple[str, ...] = ()      # 对应 --with
    without_components: tuple[str, ...] = ()      # 对应 --without

    extra_env: tuple[tuple[str, str], ...] = ()    # 仅传给 SDK 子进程

    def to_install_argv(self, home_dir: str = "/root") -> list[str]:
        argv: list[str] = []
        if self.profile:                argv += ["--profile", self.profile]
        if self.modules:                argv += ["--modules", ",".join(self.modules)]
        if self.skills:                 argv += ["--skills",  ",".join(self.skills)]
        for c in self.with_components:    argv += ["--with",    c]
        for c in self.without_components: argv += ["--without", c]
        argv += ["--home", home_dir]
        return argv

    def __post_init__(self) -> None:
        if not (self.profile or self.modules or self.skills):
            raise ValueError("PluginManifest 必须指定 profile / modules / skills 至少一个")
```

**Plan 2 修订（2026-05-22）**: `--json` flag **被暂时移除**。当前
`gg-plugins/install.sh` 不实现该 flag（传入后 silently ignored，且不会改变
stdout 内容），保留只会增加 noise；待上游 installer 真正实现结构化输出后再恢复。
`InstallReport` 信息直接从 `<install_dir>/.claude/gg/install-state.json`
（`gg.install.v1` schema）解析，不依赖 `--json`。

### 4.3 `ToolPolicy` — HITL 策略

`ToolPolicy` 是 frozen dataclass，决策矩阵由 5 个字段定义。`PATH_REQUIRED_TOOLS`
与 `AUTO_ACCEPT_TOOLS` 解耦：默认两者相同（4 个文件工具），但当调用方扩展
`auto_accept_tools`（例如把 Bash 加入）时，不应强制要求 Bash 提供 path。
路径提取由 `_extract_path()` 完成，返回 `Path | None`；当返回 `None` 时，是否升级
为 `NEEDS_HITL` 由 `path_required_tools` 决定——只对那些"语义上必须带 path"
的工具触发"missing path → NEEDS_HITL"，避免误拦无关的非文件工具。

`DANGEROUS_PATTERNS` 使用 `fnmatch` 兼容的 shell-style glob（通配符必须显式书写，
不是子字符串匹配），与 `policy.py` 实现保持一致。`_matches_dangerous()` 内部把
输入路径与 patterns 都做 `path.resolve(strict=False)` + 双侧小写后再 fnmatch，
关闭两条已知旁路：(I-1) cwd 内伪装符号链接 `innocent.txt → /work/.env` 不能逃避
危险后缀匹配；(I-2) `.ENV` / `ID_RSA` / `.PEM` 在 macOS/APFS 与 Windows/NTFS
等大小写不敏感文件系统上不能绕过策略。

```python
class Decision(StrEnum):
    ACCEPT     = "accept"
    DENY       = "deny"
    NEEDS_HITL = "needs_hitl"

class ToolPolicy:
    AUTO_ACCEPT_TOOLS   = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
    HITL_TOOLS          = frozenset({"Bash", "WebFetch", "Task"})
    NEUTRAL_TOOLS       = frozenset({"Read", "Glob", "Grep", "LS"})
    PATH_REQUIRED_TOOLS = AUTO_ACCEPT_TOOLS  # 默认与 auto-accept 集合一致；override 时可独立缩窄

    DANGEROUS_PATTERNS  = ("*.env", "*/.git/*", "*/secrets/*", "*/credentials/*", "*id_rsa*", "*.pem")

    def decide(self, tool: str, args: dict, cwd: Path) -> Decision:
        if tool in self.NEUTRAL_TOOLS:
            return Decision.ACCEPT
        if tool in self.AUTO_ACCEPT_TOOLS:
            target = self._extract_path(args)              # Path | None
            if target is None:
                if tool in self.PATH_REQUIRED_TOOLS:       return Decision.NEEDS_HITL
                return Decision.ACCEPT
            if not self._inside_cwd(target, cwd):          return Decision.NEEDS_HITL
            if self._matches_dangerous(target):            return Decision.NEEDS_HITL
            return Decision.ACCEPT
        if tool in self.HITL_TOOLS:
            return Decision.NEEDS_HITL
        return Decision.NEEDS_HITL  # 未知工具保守处理

DEFAULT_POLICY = ToolPolicy()
```

### 4.4 `RuntimeHandle` — 执行后端返回值

```python
@dataclass(frozen=True, slots=True)
class RuntimeHandle:
    backend:     str                  # "docker" | "inprocess" | "k8s"(v1.x+) | ...
    runtime_id:  str                  # 后端无关的执行实例 ID（container_id / pod_name / coroutine_id）
    transport:   SessionTransport     # 已就绪的长连接
    started_at:  datetime
    extra:       tuple[tuple[str, Any], ...] = ()   # 后端特定的额外元数据（如 docker_image_tag）
```

### 4.5 `HITLCoordinator` — Pending-Future Decision Router

进程级路由器：把 `can_use_tool` 阻塞回调里发起的"需要人工介入"请求挂起，等待外部决策（REST endpoint、IM 回调、CLI 提示、或测试桩）通过 `resolve()` 唤醒。`request()` 与 `resolve()` 通过 `req_id` 配对，同 `req_id` 重复 `request` fail-fast 抛 `ValueError`，对已被 `request()` 清理出 `_pending` 的 `req_id` 调用 `resolve()` 抛 `HITLNotPending(LookupError)`。

```python
class HITLNotPending(LookupError): ...

class HITLCoordinator:
    async def request(
        self,
        req_id: str,
        *,
        tool: str,
        args: dict[str, Any],
    ) -> Literal["accept", "deny"]: ...

    async def resolve(
        self,
        req_id: str,
        decision: Literal["accept", "deny"],
        reason: str | None = None,
    ) -> None: ...

    def pending_snapshot(self) -> dict[str, dict[str, Any]]: ...
```

**Concurrency contract:**
- `_lock` 仅保护 `_pending` 字典的 mutations；future 的 `set_result` 与 `await` 都在 lock 外（避免持锁等待协程）。
- "resolve 在 request 释锁后、await 前到达" 是常态 race：`set_result` 同步设置已就绪 future，await 立即返回，无丢决策。
- `pending_snapshot` 不持锁 — `dict` 迭代在 CPython 单线程下原子；`future.done()` 过滤排除 race 中间项，避免被 dashboard / IM 误显示。

**Reason 字段透传 (TODO Task 6+):** 当前 `request()` 只返回 `decision`，`reason` 在内部静默丢弃。接 transport `tool.decision` 控制帧 / IM 卡片之后，返回类型扩展为 `tuple[Literal["accept","deny"], str | None]` 或专属 dataclass，把 reason 透到 caller。

### 4.6 `PluginAssembler` — 真正跑 `install.sh` 的桥接

Plan 2 加入。`PluginAssembler` 是 SessionManager (handler) 与 plugin
安装策略之间的契约：

```python
@dataclass(frozen=True, slots=True)
class InstallReport:
    """`<install_dir>/.claude/gg/install-state.json` 的解析结果 +
    assembler 自测的 duration_ms。frozen 让同一 report 可安全送 dashboard /
    IM 卡片 / install.done 帧。"""
    schema_version:       str        # "gg.install.v1"
    profile_id:           str | None # selected profile, may be None
    selected_modules:     tuple[str, ...]
    included_components:  tuple[str, ...]
    excluded_components:  tuple[str, ...]
    install_root:         Path       # 来自 state 文件的 installRoot
    installed_at:         str        # ISO 8601 from state file
    duration_ms:          int        # assembler 自测，install.sh 不返回

class PluginInstallError(RuntimeError):
    def __init__(self, *, returncode: int, stderr: str, argv: tuple[str, ...]) -> None: ...

@runtime_checkable
class PluginAssembler(Protocol):
    async def prepare(self, spec: SessionSpec, *, install_dir: Path) -> InstallReport: ...
```

**调用契约（D2.1）**: SessionManager / handler 在 `executor.start(spec)`
**之前**调用 `assembler.prepare(spec, install_dir=...)`，把得到的
`InstallReport` 通过 `make_sdk_runner(install_report=report)` 透给 runner；
runner 第 0 帧就是 `install.done`。

**`InstallShellAssembler` 实现要点**:
1. 构造 argv = `<plugins_home>/install.sh` + `spec.plugins.to_install_argv(home_dir=str(install_dir))`
2. `asyncio.create_subprocess_exec(*argv, stdout=PIPE, stderr=PIPE, cwd=plugins_home, env=os.environ + spec.plugins.extra_env)`
   — 必须继承 PATH，否则 install.sh 找不到 node/npm
3. 非 0 退出 → `PluginInstallError(returncode, stderr, argv)`
4. 0 退出但 state 文件不存在 → `PluginInstallError(returncode=0, stderr="install-state.json missing")`
5. 否则解析 state 文件 → 返回 `InstallReport`

**失败传播（D2.5）**: `PluginInstallError` 直接抛给 SessionManager，
不进 runner（因为还没到 runner 阶段），handler 看到异常后可以选择
重试 / 发 install.error 帧给用户 / 上报告警。

---

## 5. Protocol 接口

### 5.1 `ExecutorBackend`

```python
@runtime_checkable
class ExecutorBackend(Protocol):
    """职责：拉起执行环境、返回就绪的 transport、收口销毁。"""
    async def start(self, spec: SessionSpec) -> RuntimeHandle: ...
    async def stop(self, handle: RuntimeHandle) -> None: ...
    async def health(self, handle: RuntimeHandle) -> bool: ...
```

**实现**：
- `InProcessExecutor`：用 `InMemoryTransport`，把 `runner.main()` 当协程跑在同一事件循环
- `DockerExecutor`：用 `UnixSocketTransport`，`docker run -d` 起容器

### 5.2 `SessionTransport`

```python
@runtime_checkable
class SessionTransport(Protocol):
    """双向 JSONL 流，连接断 = session 失败。"""
    async def send(self, frame: ControlFrame) -> None: ...
    async def recv(self) -> EventFrame: ...
    async def close(self) -> None: ...
    @property
    def is_alive(self) -> bool: ...
```

**实现**：
- `UnixSocketTransport`：基于 `asyncio.open_unix_connection`，内置心跳与 30s 超时
- `InMemoryTransport`：基于两个 `asyncio.Queue`，给 in-process 后端用

### 5.3 `PluginAssembler`

**Plan 2 修订**: 完整定义已移至 §4.6。这里仅保留 Protocol 摘要——
唯一方法 `prepare()` 真正调 `install.sh`、解析 state 文件、返回 `InstallReport`：

```python
@runtime_checkable
class PluginAssembler(Protocol):
    """职责：把 PluginManifest 翻译并真正执行 install.sh，产 InstallReport。"""
    async def prepare(self, spec: SessionSpec, *, install_dir: Path) -> InstallReport: ...
```

**实现**：`InstallShAssembler` — `build_install_argv` 直接转发到 `manifest.to_install_argv`；
`validate` 在容器外预先调 `install.sh --json --dry-run` 校验 module ID 是否合法（提交期校验，
而不是等容器起来才报错）。

---

## 6. 长连接协议（JSONL over Unix socket）

### 6.1 路径与生命周期

- 宿主侧路径：`/run/gg-relay/sessions/{session_id}.sock`（启动期 `chmod 0600`）
- 容器内路径：`/run/relay.sock`（通过 `-v` mount）
- 单连接、双向、长存活；连接断 → session 失败（无重连）
- 心跳：每 30 秒一帧 `{"type":"ping"}` / `{"type":"pong"}`；连续 3 次 ping 无 pong → 宿主侧
  标记 CRASHED + `docker rm -f`

### 6.2 帧格式（v1）

**容器 → 宿主（事件帧）**：

| `type` | 字段 | 含义 |
|---|---|---|
| `install.done` | `profile_id, modules[], duration_ms, install_root` | gg-plugins install.sh 完成（Plan 2 §4.6 InstallReport 投影） |
| `install.error` | `code, message, stderr_tail?` | install.sh 失败，stderr 右截到 2 KiB（Plan 2 新增） |
| `msg.chunk` | `data` | SDK 流式输出片段（文本、tool_use 元数据等） |
| `tool.request` | `req_id, tool, args` | PreToolUse 阻塞中，等决策 |
| `tool.result` | `req_id, ok, result` | PostToolUse 结果回报 |
| `session.end` | `status, tokens, cost` | SDK stream 结束 |
| `error` | `code, message, traceback` | 容器内异常 |
| `pong` | — | 心跳回包 |

**宿主 → 容器（控制帧）**：

| `type` | 字段 | 含义 |
|---|---|---|
| `tool.decision` | `req_id, decision, reason?` | accept / deny |
| `interrupt` | — | 紧急中断（best-effort） |
| `shutdown` | — | 优雅退出 |
| `ping` | — | 心跳探测 |

所有帧共享 `{"v":1, "type":..., "seq":<monotonic>, "ts":"<iso8601>"}` 信封。

### 6.3 不用 protobuf / gRPC 的理由

- 调试可读：`socat - UNIX-CONNECT:/run/gg-relay/sessions/X.sock | jq` 直接读
- 零 codegen：双端都直接 `json.loads/dumps`
- 不占网络端口（不动防火墙规则）
- 字段演进：`v` 字段做版本号，宿主侧能向后兼容多个 runner 镜像
- **风险**：帧大小没有 hard cap → 由协议层限制 64KB / 帧；大 payload（如长文件 diff）走分块

### 6.4 Transport Close Semantics

`SessionTransport` 关闭后的读写不对称语义（与 POSIX pipe / TCP socket 一致）：

| 操作 | close 之前 | close 之后 |
|---|---|---|
| `send(frame)` | normal | 立即 `raise TransportClosed`（等价 `EBADF`/`EPIPE`） |
| `recv()` | 阻塞或返回 frame | **先把 inbound 已缓冲的 EventFrame 全部 drain 给消费者**，看到 sentinel 才 `raise TransportClosed`（等价 `read() == 0` EOF） |
| `is_alive` | `True` | `False`，反映 *send capability*；**不**约束 `recv()` 是否有数据可读 |

实现要点：
- 关闭由"投递 sentinel + 同步设置 `_alive=False`"两步组成；sentinel 沿 inbound 队列 FIFO 传递，保证 buffered frame 不会被丢弃
- `is_alive == False && recv() returns frame` 是合法的中间态，调用方应循环 `recv()` 直到看到 `TransportClosed`
- 这条契约同样适用于 `UnixSocketTransport` / `TcpSocketTransport`：peer half-close 后本侧仍可 recv 剩余 buffered data

这条语义在 Task 7 (`InProcessExecutor` + runner) 接入时被显式验证：runner 同步发送多帧后 close runner_side，host 必须能 drain 完所有帧才看到 `TransportClosed`。

### 6.5 Tool use ID ⇄ Req ID 的 FIFO 映射（Plan 2 / Task 0 spike）

claude_code_sdk 的 `ToolPermissionContext`（host 端 `can_use_tool` 回调收到
的唯一 context）**不带** `tool_use_id` —— 只有 `signal` 和 `suggestions`
两个字段（spike 验证：`docs/sdk-message-ordering-spike.md`）。因此 host 端
HITL 自生成的 `req_id` 与 SDK 端 `AssistantMessage(ToolUseBlock(id=X))` 之间
没有直接通道，需要在 runner 内部维护一张映射表。

**算法（bidirectional defensive FIFO）**：

```python
# runner-local state:
pending_perms:      deque[(req_id, name, frozen_input)]   # can_use_tool 已 fire 但还没看到 ToolUseBlock
pending_use_blocks: deque[(tool_use_id, name, frozen_input)]  # ToolUseBlock 已收到但还没看到 can_use_tool
use_id_to_req_id:   dict[tool_use_id, req_id]             # 最终映射

# can_use_tool(tool_name, tool_input, ctx) 触发时：
fi = frozen(tool_input)
if matched_uid := pop_first_match_from(pending_use_blocks, name=tool_name, fi=fi):
    use_id_to_req_id[matched_uid] = req_id  # immediate pairing
else:
    pending_perms.append((req_id, tool_name, fi))

# AssistantMessage(content=[..., ToolUseBlock(id=X, name=N, input=I), ...]) 触发时：
fi = frozen(I)
if matched_rid := pop_first_match_from(pending_perms, name=N, fi=fi):
    use_id_to_req_id[X] = matched_rid  # immediate pairing
else:
    pending_use_blocks.append((X, N, fi))

# UserMessage(content=[..., ToolResultBlock(tool_use_id=X, ...), ...]) 触发时：
req_id = use_id_to_req_id.pop(X, "")  # "" if unmapped (defensive)
emit tool.result frame with req_id
```

**关键细节**：

1. **bidirectional** —— SDK 内 `_read_messages` 把 `control_request` 经
   `task_group.start_soon` 并发派发，把 regular messages 顺序入流。两路
   交互的相对顺序由 CLI 决定，**无法保证 can_use_tool 一定先于
   AssistantMessage(ToolUseBlock) 到达**。spike 报告 §2 详述。
2. **FIFO** —— 当同一 `(name, input)` 重复出现时（LLM 连续两次同样的
   tool call），按到达顺序 pair：第一个 perm 配第一个 use block。
3. **frozen_input** —— `frozenset((k, json.dumps(v, sort_keys=True,
   default=str)) for k, v in d.items())`；嵌套 dict / list 经 `json.dumps`
   规范化，保证 `{"a":1,"b":[2,3]}` 和 `{"b":[2,3],"a":1}` 同 key。
4. **defensive fallback** —— `use_id_to_req_id.pop(X, "")` 永不抛；上游
   丢帧 / 协议 garble 时 host 看到 `tool.result` 但 `req_id == ""`，能
   继续渲染但 trace 上挂不上对应的 request。Plan 2 接受这个损失，
   Plan 4 加埋点告警。
5. **edge case** — `can_use_tool` 返回 deny → CLI 不会发对应 ToolUseBlock
   → `pending_perms` 里那条记录残留至 session 结束。一次 session 最多
   `max_turns` 条，可接受；leak 可观察后再优化。

---

## 7. HITL 实时流程

### 7.1 序列（不依赖 SDK interrupt/resume）

```
container.runner            host.client            host.HITLCoord       store / IM
       │                          │                       │                   │
       │ PreToolUse(tool, args)   │                       │                   │
       │ req_id = uuid()          │                       │                   │
       │ tool.request ────────────▶                       │                   │
       │                          │ ToolPolicy.decide()   │                   │
       │                          │                       │                   │
       │            ┌─────── ACCEPT ───────┐              │                   │
       │ ◀──tool.decision: accept──        │              │                   │
       │                          │                       │                   │
       │            └─── NEEDS_HITL ───────┐              │                   │
       │                          │ publish HITLRequested ▶ persist (durable) │
       │                          │                       │ IMSubscriber ────▶│ 发卡片
       │                          │                       │                   │ 用户点击
       │                          │     ◀── REST /hitl/{id}/approve ──────────│
       │                          │ HITLCoord.wake(req_id)│                   │
       │ ◀──tool.decision: accept │                       │                   │
       │ 继续 SDK 流              │                       │                   │
       │ PostToolUse(result)      │                       │                   │
       │ tool.result ─────────────▶ publish ToolCallResolved                  │
```

**关键性质**：
- 容器内 runner 阻塞在 `socket.read_until(req_id)` 上 → 零 CPU 等待
- HITL 决策落 store 为 durable（PLAN.md §3 不变量 #6）
- 用户隔多久回都行，不超时（除非 `SessionSpec.timeout_s` 触发）
- 同一 session 内可并发多个 `req_id`（手机端可批量审批）

### 7.2 SDK 能力 spike（合并 PLAN.md P0-9）

| spike 项 | 目的 | 决策影响 |
|---|---|---|
| `ClaudeSDKClient` 是否支持 `PreToolUse` 同步阻塞 callback | 7.1 流程的 §3 步骤可行性 | 否 → 降级方案 A：在 runner.bridge 截 message stream，遇到 `tool_use` 块先按住 |
| `ClaudeSDKClient` 是否支持 `interrupt()/resume()` | 兜底 `interrupt` 控制帧 | 否 → 该控制帧降级为 `shutdown`（kill 整个 session） |
| Hook callback 是否可异步 | 决定 client.py 实现风格 | 否 → 用 `asyncio.run_coroutine_threadsafe` 桥到事件循环 |

spike 脚本：`scripts/spike_sdk_interrupt.py`（PLAN.md 已规划），验收输出写入 `docs/sdk-spike-report.md`。

---

## 8. 基础镜像设计

### 8.1 Dockerfile 骨架（`deploy/docker/runner.Dockerfile`）

```dockerfile
# ── Stage 1: node deps ────────────────────────────────────────────
FROM node:20-slim AS plugins-deps
ARG GG_PLUGINS_REF=main
WORKDIR /opt/gg-plugins
RUN git clone --depth=1 --branch ${GG_PLUGINS_REF} \
      https://github.com/<org>/gg-plugins.git .
RUN npm install --no-audit --no-fund

# ── Stage 2: python runtime ───────────────────────────────────────
FROM python:3.12-slim AS runtime
COPY --from=plugins-deps /opt/gg-plugins /opt/gg-plugins

# claude CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash nodejs curl ca-certificates \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

# gg-relay runner（只装 runner 子包所需，不装 FastAPI 等宿主侧依赖）
COPY src/gg_relay /opt/gg-relay/src/gg_relay
COPY pyproject.toml /opt/gg-relay/
RUN pip install --no-cache-dir /opt/gg-relay

# 默认禁网，由 docker run --network 控制
USER nobody
ENTRYPOINT ["python", "-m", "gg_relay.runner"]
```

**关键决策**：
- `GG_PLUGINS_REF` build arg = gg-plugins commit sha；镜像 tag 与 sha 一一对应
- `node_modules` 烘进 stage 1，runtime 完全自包含（决策 D1-i）
- runner 子包独立可装（pip extras `[runner]`），不拖 fastapi/uvicorn/sqlalchemy 进容器
- ENTRYPOINT 固定为 runner；CMD 由 DockerExecutor 在 `docker run` 时提供 `--socket /run/relay.sock`

### 8.2 镜像 tag 策略

- 生产：`gg-relay-runner:<gg-plugins-tag>`（如 `gg-relay-runner:v1.4.2`）
- 测试：`gg-relay-runner:<gg-plugins-tag>-test`
- 本地开发：`gg-relay-runner:dev`（手动 build）
- gg-relay 配置项 `RELAY_RUNNER_IMAGE` 指定默认 tag

### 8.3 CI 触发

```yaml
# .github/workflows/release-runner.yml (新增)
on:
  repository_dispatch:    # gg-plugins repo 打 release tag 后 webhook 触发
    types: [gg-plugins-release]
jobs:
  build:
    steps:
      - uses: docker/build-push-action@v5
        with:
          file: deploy/docker/runner.Dockerfile
          build-args: |
            GG_PLUGINS_REF=${{ github.event.client_payload.tag }}
          tags: ghcr.io/<org>/gg-relay-runner:${{ github.event.client_payload.tag }}
          push: true
```

---

## 9. 对 PLAN.md 的修订

| PLAN.md 位置 | 修订 |
|---|---|
| §3 架构图 | claude CLI 不再画在宿主层；新增"ExecutorBackend"抽象层 + "container: gg-relay-runner"分组 |
| §5 模块架构 | `session/` 下新增 `executor/`、`assembly/`、`transport/`、`runner/`、`hitl/` 五个子包 |
| §6 P0-9 | spike 范围**缩窄**为「PreToolUse 同步阻塞能力 + Hook 异步支持」；interrupt/resume 降为可选 |
| §6 P0 | 新增 **P0-13**：`ExecutorBackend` Protocol + `InProcessExecutor` 最小实现（与 P1 解耦） |
| §6 P0 | 新增 **P0-14**：`SessionTransport` Protocol + `InMemoryTransport`（in-process 后端依赖） |
| §6 P1 | 新增 **P1-9**：`DockerExecutor` + `runner.Dockerfile` + multi-stage build |
| §6 P1 | 新增 **P1-10**：`UnixSocketTransport` + 心跳 + 帧 v1 协议 |
| §6 P1 | 新增 **P1-11**：`InstallShAssembler` + `install.sh --dry-run` 预校验 |
| §6 P1 | 新增 **P1-12**：`ToolPolicy` + `HITLCoordinator` + 与 §11 IM 的对接复用 |
| §8 数据模型 | 新增 `SessionSpec` / `PluginManifest` / `ToolPolicy` / `Decision` / `RuntimeHandle` |
| §14 SDK 契约 | 增补：runner 内 `ClaudeSDKClient` 实例归 runner.bridge 独占；宿主侧 `client.py` 不再直接实例化 SDK |
| §15 风险登记 | 见下表追加 R12–R17 |

### §15 追加风险

| ID | 风险 | 严重 | 概率 | 缓解 |
|---|---|---|---|---|
| R12 | Docker daemon 不可用 / 权限不足 | HIGH | LOW | 启动期校验 `docker info`；dev 自动降级 in-process（带警告） |
| R13 | gg-plugins 升级改了 install.sh CLI 或 module 命名 | MEDIUM | MEDIUM | 容器启动时 `install.sh --list-modules --json` 校验 manifest，错误立即报；CI pin gg-plugins commit |
| R14 | npm install 失败 / 慢 | MEDIUM | LOW | 决策 D1-i 烘 node_modules 入镜像 + 容器 `--network none` |
| R15 | install state 与 manifest 漂移 | LOW | MEDIUM | install 后回灌 state JSON 入事件 + store，便于审计 |
| R16 | 长连接半关 / 容器静默 hang | MEDIUM | MEDIUM | 30s 心跳 + 3 次失败 → CRASHED + `docker rm -f` |
| R17 | SDK 不支持 PreToolUse 同步阻塞 | HIGH | MEDIUM | spike 前置；fallback 在 runner.bridge 截 message stream |

---

## 10. 模块文件清单（增量于 PLAN.md §7）

```
src/gg_relay/
└── session/
    ├── spec.py                   ★ SessionSpec / PluginManifest / RuntimeHandle / Decision
    │
    ├── client.py                 ★ GgRelayClaudeClient — 宿主侧协调器
    │                                (持 transport, 调 ToolPolicy, 触发 HITLCoord)
    │
    ├── executor/                 ★
    │   ├── __init__.py
    │   ├── protocol.py           # ExecutorBackend Protocol
    │   ├── inprocess.py          # InProcessExecutor (InMemoryTransport)
    │   └── docker.py             # DockerExecutor (UnixSocketTransport, docker-py 或 subprocess)
    │
    ├── transport/                ★
    │   ├── __init__.py
    │   ├── protocol.py           # SessionTransport / ControlFrame / EventFrame TypedDict
    │   ├── unix_socket.py        # UnixSocketTransport + 心跳
    │   └── inmemory.py           # InMemoryTransport (双 asyncio.Queue)
    │
    ├── assembly/                 ★
    │   ├── __init__.py
    │   ├── protocol.py           # PluginAssembler / ValidationResult
    │   └── install_sh.py         # InstallShAssembler
    │
    ├── runner/                   ★ 容器内可执行入口（gg-relay-runner pip extra）
    │   ├── __init__.py
    │   ├── __main__.py           # python -m gg_relay.runner
    │   ├── main.py               # 编排：install → spawn SDK → bridge
    │   ├── bridge.py             # ClaudeSDKClient ↔ socket 双向桥
    │   └── install.py            # 调用 install.sh 的薄封装
    │
    ├── hitl/                     ★
    │   ├── __init__.py
    │   ├── policy.py             # ToolPolicy
    │   └── coordinator.py        # HITLCoordinator (与 EventBus / store 联动)
    │
    ├── manager.py                # SessionManager — 不变接口，改为调 ExecutorBackend
    └── recovery.py               # 不变（PLAN.md §6 P0-10）

deploy/docker/
└── runner.Dockerfile             ★ multi-stage build

.github/workflows/
└── release-runner.yml            ★ gg-plugins release tag 触发镜像 build
```

---

## 11. 验收准则

### 11.1 单元 / 集成测试（PLAN.md §7 tests/ 增量）

```
tests/unit/session/
├── test_spec.py                   # PluginManifest 校验 + to_install_argv
├── test_policy.py                 # ToolPolicy 决策矩阵（cwd 内/外、黑名单、未知工具）
├── test_inprocess_executor.py     # InProcessExecutor 全链路（mock SDK）
└── test_transport_inmemory.py     # InMemoryTransport 双向收发 + close 行为

tests/unit/session/runner/
├── test_install_sh.py             # InstallShAssembler.build_install_argv
└── test_bridge_pretool_block.py   # PreToolUse 阻塞 + decision 回灌

tests/integration/
├── test_full_hitl_cycle.py        # InProcessExecutor + tool.request → /hitl/approve → tool.decision
└── test_docker_executor.py        # @pytest.mark.docker 真起容器（CI 中带 docker）
```

### 11.2 验收命令

| 验收项 | 命令 / 期望 |
|---|---|
| 镜像可启动 | `docker run --rm gg-relay-runner:dev claude --version` 输出版本号 |
| install.sh 可调用 | 容器内 `bash /opt/gg-plugins/install.sh --profile minimal --home /tmp/t --dry-run --json` 输出 plan |
| in-process 后端跑通 | `await session_manager.submit(SessionSpec(executor="inprocess", plugins=PluginManifest(profile="minimal"), prompt="echo hi", cwd=tmp_path))` → COMPLETED |
| docker 后端跑通 | 同上 `executor="docker"`，结束后 `docker ps -a` 看不到该容器 |
| HITL 流程跑通 | trigger NEEDS_HITL → SSE 上看到 `HITLRequested` → REST approve → 看到 `HITLResolved` → session COMPLETED |
| 路径策略正确 | `policy.decide("Write", {"file_path": "/work/main.py"}, cwd=Path("/work"))` == `ACCEPT` |
| 路径策略保守 | `policy.decide("Write", {"file_path": "/etc/passwd"}, cwd=Path("/work"))` == `NEEDS_HITL` |
| 心跳兜底 | 暴力 kill 容器内 runner 进程 → 90s 内宿主把 session 标记为 CRASHED |

---

## 12. Open Questions（待 spike 或实施期确认）

1. **`claude-code-sdk` Python hook API 形态** — 需要 P0 spike 验证。是否暴露 `on(event, callback)`
   形式？callback 是否可异步？能否在 callback 内同步阻塞？决策影响 7.2 节降级方案是否触发。
2. **gg-plugins 仓库 URL & 私有/公开** — `runner.Dockerfile` 第 4 行的 git clone URL 需要确认。
   若私有，build 时需要注入 SSH key 或 GHCR token。
3. **`install.sh` 是否会动 `~/.claude/settings.json`** — `hooks-runtime` 模块会 merge 进 settings.json，
   多次安装的合并语义需要确认（决策 D2-a 单版本下应该不冲突，但 spec 想表态）。
4. **容器内 claude CLI 与 `~/.claude/` 的对应** — `--home /root` 装到 `/root/.claude/`，
   claude CLI 默认读 `$HOME/.claude/` → runner 进程的 HOME 必须是 `/root`，需要 Dockerfile 显式 `ENV HOME=/root`。
5. **secrets 注入** — `extra_env` 怎么传到 SDK 子进程？是 `docker run -e` 还是通过 socket
   首帧传？倾向后者（避免 env 泄露给 install.sh 与 docker inspect）。
6. **资源配额** — `docker run` 时是否默认 `--memory 2g --cpus 1.0`？handler 是否可在 `SessionSpec`
   覆盖？v1 倾向硬编码默认值，不开放。
7. **大 payload 分块策略** — `tool.request.args` 如果包含 `Write` 的整个文件内容（可能 MB 级），
   会撑爆 §6.2 单帧 64KB 限制。v1 倾向 args 超过 4KB 时只发**摘要 + hash**给宿主用于策略判定，
   实际内容仍在容器内执行；HITL 卡片渲染只展示 diff 摘要。需要在 transport spec v1.1 细化。
8. **`SessionRecord` schema 扩展** — PLAN.md §8 的 `SessionRecord` 是否需要增加 `runner_image_tag`、
   `install_state_hash`、`transport_path` 字段？倾向"是"（便于审计），但需要 alembic 迁移。

---

## 13. 不在本 spec 范围

- IM 卡片样式、多人审批、审批超时升级链 — 沿用 PLAN.md §11，本 spec 只对接 `HITLRequested` 事件
- **`K8sExecutor` 实现** — 仅在 §1.4 声明为预留扩展点；具体 Pod 模板、跨 Pod transport、
  Service mesh 接入等走独立 spec（建议在 v1 稳定后启动）
- gg-plugins 自身的开发流程
- claude CLI 的 fork / patch（如有需要走单独 spec）

---

*Spec 完。下一步：用户 review → 进入 `writing-plans` 拆实施步骤。*
