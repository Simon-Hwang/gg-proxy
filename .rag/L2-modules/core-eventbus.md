---
id: core-eventbus
level: L2
type: module
title: "Core 模块 — EventBus + Domain + Events"
path: src/gg_relay/core/
tags: [python, event-bus, domain, pub-sub, frozen-dataclass]
domain: [event-driven, pub-sub, state-machine, backpressure]
intent:
  - "查 EventBus 的 publish/subscribe 用法"
  - "了解 RelayEvent 层级和 delivery tier 策略"
  - "定位 SessionState 状态机和合法转换表"
source_paths:
  - src/gg_relay/core/event_bus.py
  - src/gg_relay/core/events.py
  - src/gg_relay/core/domain.py
symbols:
  - EventBus
  - RelayEvent
  - SessionState
  - LEGAL_TRANSITIONS
  - DeliveryTier
  - frame_to_event
  - SessionCreated
  - SessionStateChanged
  - HITLRequested
parent: gg-relay-system
analyzer: style
token_estimate: 2500
summary: >
  进程内 async pub/sub 总线（topic-keyed fan-out + delivery tier 背压）、RelayEvent 类层级和 SessionState 状态机
graph_node_id: core-eventbus
created: 2026-05-25
updated: 2026-05-25
confidence: high
---

# Core 模块 — EventBus + Domain + Events

## 职责

`core/` 是零外部依赖的领域层，提供：
1. **EventBus** — 进程内 async topic-keyed fan-out，支持 typed + legacy string 两种 publish 形式
2. **RelayEvent 层级** — 11 个 frozen dataclass 子类覆盖全部 wire frame 类型
3. **SessionState 状态机** — StrEnum + `LEGAL_TRANSITIONS` dict 定义合法边
4. **Protocol 定义** — `DurableEventStore`, `EventBusBackend` 等跨模块接口

## EventBus 对外接口

```python
class EventBus:
    def subscribe(self, topic: type[RelayEvent] | str, *, maxsize=1000) -> AsyncIterator[Any]
    async def publish(self, event: RelayEvent) -> None          # typed 形式
    async def publish(self, topic: str, payload: Any) -> None   # legacy 2-arg
    async def replay_after(*, last_seq: int | None, limit=1000) -> AsyncIterator[RelayEvent]
    async def subscribe_all(*, after_seq=None, limit=1000) -> AsyncIterator[RelayEvent]
    async def close() -> None
```

**Wildcard 订阅**: `bus.subscribe("*")` 接收所有 topic 事件。

## Delivery Tier 策略

| Tier | 行为 | 典型事件 |
|------|------|---------|
| `lossy` | 队列满 → 丢弃最旧项，递增 drop counter | SessionOutputChunk, Heartbeat, InstallDone |
| `durable` | 队列满 → 阻塞 publisher 最多 `durable_block_timeout_s` (1s)，超时后仍丢弃并计数 | SessionCreated, SessionStateChanged, HITLRequested/Resolved, ToolRequested/Resolved |

Durable 事件在 fan-out 前先通过 `durable_store.persist()` 写入 `events` 表。

## RelayEvent 子类

| 类 | delivery_tier | 含义 |
|---|---|---|
| `SessionCreated` | durable | 新会话行写入 |
| `SessionStateChanged` | durable | 生命周期状态转换 |
| `SessionOutputChunk` | lossy | SDK msg.chunk frame |
| `SessionCompleted` | durable | 终态 + token/cost 汇总 |
| `HITLRequested` | durable | 工具调用需人工审批 |
| `HITLResolved` | durable | 审批决定已做出 |
| `ToolRequested` | durable | 所有 tool 调用（含自动通过） |
| `ToolResolved` | durable | tool 调用结果 |
| `InstallDone` | lossy | 插件安装完成 |
| `InstallError` | durable | 插件安装/运行时错误 |
| `Heartbeat` | lossy | runner 心跳 |
| `KeyInvalidated` | durable | dashboard key 轮换广播 |

## Frame → Event 转换

`frame_to_event(session_id, frame_dict)` 通过 `_FRAME_TO_EVENT` dispatch table 将 wire frame dict 提升为 typed RelayEvent：

```python
_FRAME_TO_EVENT = {
    "msg.chunk": _from_msg_chunk,
    "tool.request": _from_tool_request,
    "tool.result": _from_tool_result,
    "install.done": _from_install_done,
    "install.error": _from_install_error,
    "error": _from_error_frame,
    "session.end": _from_session_end,
    "pong": _from_pong,
}
```

## SessionState 状态机

```
QUEUED → RUNNING → PAUSED → RUNNING → COMPLETED/FAILED/CANCELLED/INTERRUPTED
                 → COMPLETED/FAILED/CANCELLED/INTERRUPTED
```

终态（COMPLETED/FAILED/CANCELLED/INTERRUPTED）无出边。校验函数：
```python
def is_legal_transition(from_state, to_state) -> bool
```

## 扩展点

- 新增事件：在 `events.py` 添加 frozen dataclass subclass + 更新 `RelayEventT` union
- 新增 delivery tier：修改 `DeliveryTier` Literal + `_dispatch()` 分支
- 新增 wire frame：在 `_FRAME_TO_EVENT` 添加 factory function

## source_paths

- src/gg_relay/core/event_bus.py
- src/gg_relay/core/events.py
- src/gg_relay/core/domain.py
- src/gg_relay/core/protocol.py
- src/gg_relay/core/exceptions.py
