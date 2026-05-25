---
id: gg-relay-system
level: L1
type: system-style
title: "gg-relay 系统架构与编码风格指南"
path: src/gg_relay/
tags: [python, fastapi, async, protocol, frozen-dataclass]
domain: [architecture, coding-style, dependency-injection, event-driven]
intent:
  - "查 gg-relay 整体架构分层与模块边界"
  - "了解编码规范和扩展方式"
  - "新增功能时定位入口点和注入方式"
source_paths:
  - src/gg_relay/api/main.py
  - src/gg_relay/core/
  - src/gg_relay/config.py
symbols:
  - Config
  - create_app
  - lifespan
  - EventBus
  - SessionManager
  - RelayEvent
  - typing.Protocol
parent: L0-overview
analyzer: style
token_estimate: 2800
summary: >
  Python 后端服务的分层架构、事件驱动设计哲学、Protocol 接口规范与编码红线
graph_node_id: gg-relay-system
created: 2026-05-25
updated: 2026-05-25
confidence: high
---

# gg-relay 系统架构与编码风格指南

## 分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│ API Layer (api/)                                                │
│   FastAPI routers + middleware chain                            │
│   Session → DashboardCookie → APIKey → AuditFallback           │
│   → RateLimit → Logging → router                              │
├─────────────────────────────────────────────────────────────────┤
│ Application Layer (session/)                                    │
│   SessionManager orchestrates lifecycle                         │
│   HITLCoordinator manages approval futures                     │
│   ExecutorBackend abstracts runtime (inprocess/docker/k8s)     │
├─────────────────────────────────────────────────────────────────┤
│ Domain Layer (core/)                                            │
│   RelayEvent hierarchy (frozen dataclasses)                    │
│   SessionState enum + LEGAL_TRANSITIONS                        │
│   EventBus (topic-keyed fan-out)                               │
│   Protocol definitions (DurableEventStore, EventBusBackend)    │
├─────────────────────────────────────────────────────────────────┤
│ Infrastructure Layer (store/ + cluster/ + tracing/)             │
│   SQLAlchemy Core async repository                             │
│   Redis-backed EventBus / rate limiter                         │
│   OTel + Prometheus + task-trace                               │
└─────────────────────────────────────────────────────────────────┘
```

## 依赖注入模式

所有服务通过 `lifespan()` 异步上下文管理器统一初始化，挂载到 `app.state`:

```python
# src/gg_relay/api/main.py — lifespan() 摘要
engine = make_async_engine(cfg.database_url, ...)
store = SessionRepository(engine)
bus = await build_event_bus(cfg, durable_store=durable_store, ...)
coordinator = HITLCoordinator(store=store)
manager = SessionManager(
    executor_factory=executor_factory,
    store=store, bus=bus, coordinator=coordinator, ...
)
app.state.manager = manager
app.state.bus = bus
# ... routers 从 request.app.state 读取依赖
```

## Protocol 接口规范

**所有跨模块边界使用 `typing.Protocol`（结构化子类型）**:

| Protocol | 位置 | 实现者 |
|----------|------|--------|
| `DurableEventStore` | `core/protocol.py` | `SqlAlchemyDurableEventStore` |
| `EventBusBackend` | `core/protocol.py` | `EventBus`, `RedisStreamEventBus` |
| `SessionTransport` | `session/transport/protocol.py` | `InMemoryTransport`, `TcpTransport`, `UnixSocketTransport` |
| `ExecutorBackend` | `session/executor/protocol.py` | `InProcessExecutor`, `DockerExecutor`, `K8sJobExecutor` |
| `PluginAssembler` | `session/plugins/protocol.py` | `InstallShellAssembler`, `_NoopAssembler` |
| `IMBackend` | `im/protocol.py` | `FeishuBackend` |
| `HITLStore` | `store/protocol.py` | `SessionRepository` |

## 事件驱动设计哲学

EventBus 是唯一 fan-out 机制。所有跨组件通信都是 typed events：

```python
# 发布（SessionManager 内部）
await self._bus.publish(SessionStateChanged(
    session_id=sid,
    from_state="running", to_state="paused",
))

# 消费（任何 subscriber）
async for event in bus.subscribe(SessionStateChanged):
    handle(event)
```

**Delivery Tier 策略**:
- `lossy` — 队列满时丢弃最旧项（OTel/metrics/heartbeat）
- `durable` — 队列满时阻塞 publisher 最多 `durable_block_timeout_s`，持久化到 events 表（SSE replay）

## 核心编码红线（DOs / DON'Ts）

### DO:
- 所有 domain 对象用 `@dataclass(frozen=True, slots=True)`
- 配置全部通过 `Config` 类 + `RELAY_*` env var
- 跨模块通信仅通过 EventBus publish
- 敏感数据在 SessionManager boundary 通过 RedactionEngine 脱敏
- API keys 只存 SHA256 hash，明文仅 POST 响应返回一次
- 所有状态转换用 `LEGAL_TRANSITIONS` 校验

### DON'T:
- 禁止 mutable dataclass / 直接修改 domain 对象
- 禁止 subscriber 直接调用其他 subscriber
- 禁止在 store 层看到明文 secrets（RedactionEngine 已在上层完成）
- 禁止使用 `query()` shorthand — 必须用 `ClaudeSDKClient` 完整调用
- 禁止在 middleware 内做 DB write（audit 除外，通过 AuditService）

## 标准扩展工作流

### 新增 API 端点

1. 在 `api/routers/` 下创建新 router 文件
2. 在 `create_app()` 中 `include_router()`
3. 如需权限控制，使用 `require_role` 依赖
4. 如涉及状态变更，写 audit 行（通过 `audit_service.record()`）

### 新增 Event 类型

1. 在 `core/events.py` 添加 frozen dataclass subclass
2. 更新 `RelayEventT` union 和 `__all__`
3. 如有 wire frame 对应，添加 `_FRAME_TO_EVENT` 条目
4. 选择 `delivery_tier`（lossy vs durable）

### 新增 Executor 后端

1. 实现 `ExecutorBackend` Protocol
2. 在 `_build_executor_factory()` 添加 `kind` 分支
3. 在 `Config` 中添加 executor_kind literal variant

## 测试约定

- pytest + pytest-asyncio (auto mode)
- 88% 最低覆盖率 (`--cov-fail-under=88`)
- `conftest.py` 提供 `FakeStore`, `FakeTransport`, `FakeBus` fixtures
- Integration tests 标记: `@pytest.mark.requires_docker`, `@pytest.mark.requires_sdk`

## source_paths

- src/gg_relay/api/main.py
- src/gg_relay/core/
- src/gg_relay/core/protocol.py
- src/gg_relay/config.py
- src/gg_relay/session/executor/protocol.py
- src/gg_relay/session/transport/protocol.py
