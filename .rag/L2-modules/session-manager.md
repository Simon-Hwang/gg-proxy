---
id: session-manager
level: L2
type: module
title: "Session 模块 — SessionManager + Executors + HITL"
path: src/gg_relay/session/
tags: [python, async, session-lifecycle, executor, hitl, pause-resume]
domain: [session-management, concurrency, executor, human-in-the-loop, pause-resume]
intent:
  - "查 SessionManager 的 submit/pause/resume/cancel 工作流"
  - "了解 executor 后端（inprocess/docker/k8s）如何切换"
  - "定位 HITL 审批流程的协调逻辑"
  - "修改 session 并发控制或超时策略"
source_paths:
  - src/gg_relay/session/manager.py
  - src/gg_relay/session/executor/
  - src/gg_relay/session/hitl/
symbols:
  - SessionManager
  - ExecutorBackend
  - HITLCoordinator
  - ToolPolicy
  - SessionSpec
  - InProcessExecutor
  - DockerExecutor
  - K8sJobExecutor
  - ControlChannel
  - WireBridge
parent: gg-relay-system
analyzer: style
token_estimate: 2800
summary: >
  进程级会话编排器，管理 submit/pause/resume/cancel 生命周期、executor 后端选择和 HITL 审批协调
graph_node_id: session-manager
created: 2026-05-25
updated: 2026-05-25
confidence: high
---

# Session 模块 — SessionManager + Executors + HITL

## 职责

`session/` 是 gg-relay 的核心应用层，包含：
1. **SessionManager** — 进程级编排器，管理会话生命周期（submit → run → pause/resume → terminal）
2. **Executors** — 抽象执行后端（InProcess / Docker / K8s Job）
3. **HITL** — Human-in-the-loop 审批协调（Coordinator + Policy）
4. **Transport** — 进程间/网络通信协议（InMemory / TCP / UnixSocket）
5. **Plugins** — gg-plugins 安装/组装器
6. **Runner** — SDK bridge + control loop

## SessionManager 对外接口

```python
class SessionManager:
    async def submit(spec, *, runtime_ctx, api_key_id, owner, description, parent_session_id) -> str
    async def list(*, status, tag, limit, after) -> (list[SessionSummary], cursor)
    async def get(sid, *, frames_limit, frames_offset) -> SessionDetail
    async def retry(sid, *, actor) -> str
    async def cancel(sid, *, reason) -> None
    async def pause(sid, *, reason) -> None
    async def resume(sid, *, hint) -> None
    async def shutdown(*, grace_period_s, paused_action) -> None
```

## 并发控制

- **信号量** (`asyncio.Semaphore(max_concurrent)`) — 限制同时运行的 session 数（默认 10）
- **Pause 释放 slot** — `pause()` 释放信号量让排队的 submit 继续；`resume()` 重新获取
- **Paused caps** — 全局 `max_paused=50` + per-API-key `max_paused_per_api_key=20`
- **乐观锁** — `sessions.version` 列 + `_update_status_locked()` 带 1 次 jitter 重试

## Executor 后端

| Backend | Config | 通信 | 使用场景 |
|---------|--------|------|---------|
| `InProcessExecutor` | `executor_kind=inprocess` | InMemoryTransport | 开发/单进程 |
| `DockerExecutor` | `executor_kind=docker` | UnixSocketTransport (NDJSON) | 隔离执行 |
| `K8sJobExecutor` | `executor_kind=k8s_job` | TcpTransport + auth token | 集群部署 |

Factory 模式：`_build_executor_factory(cfg)` 返回 `ExecutorFactory` callable。

## HITL 审批流

```
ToolPolicy.evaluate(tool, args)
  → ALLOW      → auto-accept, ToolRequested event
  → NEEDS_HITL → coordinator.request(req_id) blocks
                  → dashboard/API calls coordinator.resolve(req_id, decision)
                  → future resolves, runner continues
```

- **HITLCoordinator** — in-process future 路由器，`request()` 阻塞，`resolve()` 唤醒
- **Defence-in-depth** — coordinator 可选 `store` 引用，resolve 前校验 DB 行状态

## Pause/Resume 状态

```
pause():  bridge.pause() → ack → row→PAUSED → release sem → arm timer
resume(): sem.acquire(timeout) → bridge.resume() → ack → row→RUNNING
timeout:  paused_timeout_watchdog cancels session
```

Timer 可通过 `_arm_paused_timer(remaining_s)` 从断点恢复。

## 内部结构

```
session/
├── manager.py          # SessionManager 核心编排
├── spec.py             # SessionSpec, PluginManifest, RuntimeHandle
├── control.py          # ControlChannel (asyncio.Queue pair)
├── recovery.py         # 启动时恢复中断的 session + paused timers
├── client.py           # make_sdk_runner() — claude-code-sdk wrapper
├── frames.py           # Frame type definitions
├── executor/
│   ├── protocol.py     # ExecutorBackend Protocol
│   ├── inprocess.py    # InProcessExecutor
│   ├── docker.py       # DockerExecutor (aiodocker)
│   ├── k8s_job.py      # K8sJobExecutor
│   └── k8s_client.py   # kubernetes-asyncio wrapper
├── hitl/
│   ├── coordinator.py  # HITLCoordinator
│   └── policy.py       # ToolPolicy (allowlist/denylist)
├── runner/
│   ├── bridge.py       # WireBridge (Docker transport drain + HITL)
│   ├── wire_runner.py  # Wire protocol runner
│   ├── inprocess_control.py  # InProcessBridge (pause/resume ack)
│   └── proxy_client.py # Proxy integration
├── transport/
│   ├── protocol.py     # SessionTransport Protocol
│   ├── inmemory.py     # InMemoryTransport
│   ├── tcp.py          # TcpTransport
│   └── unixsocket.py   # UnixSocketTransport
└── plugins/
    ├── protocol.py     # PluginAssembler Protocol
    └── install_shell.py # InstallShellAssembler
```

## 扩展点

- 新增 executor：实现 `ExecutorBackend` Protocol + 在 factory 添加分支
- 新增 HITL policy rule：扩展 `ToolPolicy.evaluate()` 判断逻辑
- 新增 transport：实现 `SessionTransport` Protocol

## source_paths

- src/gg_relay/session/manager.py
- src/gg_relay/session/executor/protocol.py
- src/gg_relay/session/executor/inprocess.py
- src/gg_relay/session/executor/docker.py
- src/gg_relay/session/executor/k8s_job.py
- src/gg_relay/session/hitl/coordinator.py
- src/gg_relay/session/hitl/policy.py
- src/gg_relay/session/transport/protocol.py
- src/gg_relay/session/spec.py
- src/gg_relay/session/recovery.py
