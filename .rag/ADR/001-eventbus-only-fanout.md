---
id: adr-001-eventbus
level: ADR
type: adr
title: "ADR-001: EventBus 作为唯一 fan-out 机制"
tags: [architecture, event-bus, decoupling]
domain: [event-driven, architecture, decoupling]
intent:
  - "为什么不允许 subscriber 直接调用其他 subscriber"
  - "EventBus 设计决策的上下文和理由"
source_paths:
  - src/gg_relay/core/event_bus.py
  - docs/architecture.md
symbols:
  - EventBus
  - RelayEvent
  - publish
  - subscribe
graph_node_id: adr-001-eventbus
token_estimate: 800
summary: >
  EventBus 是所有跨组件通信的唯一通道，禁止直接调用以保证松耦合和可测试性
created: 2026-05-25
updated: 2026-05-25
confidence: high
---

# ADR-001: EventBus 作为唯一 fan-out 机制

## 上下文

gg-relay 需要将 SessionManager 产生的事件分发给多个消费者（OTel、Prometheus、SSE、IM、审计）。早期原型中 SessionManager 直接调用各个 subscriber，导致循环依赖和难以测试。

## 决策

**所有跨组件通信仅通过 EventBus publish typed events。** 生产者和消费者通过事件类型名（class name）解耦。

## 理由

1. **松耦合** — 添加新消费者不需要修改生产者代码
2. **可测试** — 测试只需 mock bus.publish，验证事件内容
3. **可观测** — drop counter 和 delivery tier 策略集中在 bus 内
4. **可扩展** — Plan 9 Redis Streams bus 无需修改任何 subscriber

## 后果

- 必须维护 RelayEvent 层级（每新增事件类型一次编辑）
- Debug 时需要看 bus drop metrics 而非直接 call stack
- 事件序列不保证跨 topic 全局有序（per-subscriber queue 有序）

## 关联代码

- `src/gg_relay/core/event_bus.py` — EventBus 实现
- `src/gg_relay/core/events.py` — 11 个 RelayEvent 子类
- `src/gg_relay/session/manager.py:_persist_frame()` — 唯一生产者入口
