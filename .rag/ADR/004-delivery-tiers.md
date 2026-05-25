---
id: adr-004-delivery-tiers
level: ADR
type: adr
title: "ADR-004: Delivery Tier 事件分级策略"
tags: [architecture, event-bus, lossy, durable, backpressure]
domain: [event-driven, reliability, persistence, backpressure]
intent:
  - "为什么区分 lossy 和 durable 事件"
  - "理解 persist-before-fanout 的可靠性保证"
source_paths:
  - src/gg_relay/core/events.py
  - src/gg_relay/core/event_bus.py
symbols:
  - DeliveryTier
  - durable
  - lossy
  - DurableEventStore
  - durable_block_timeout_s
graph_node_id: adr-004-delivery-tiers
token_estimate: 600
summary: >
  EventBus 事件分为 lossy（可丢弃，UI 追赶）和 durable（先持久化，SSE replay）两个投递等级
created: 2026-05-25
updated: 2026-05-25
confidence: high
---

# ADR-004: Delivery Tier 事件分级策略

## 上下文

不是所有事件都同样重要：
- `SessionOutputChunk`（msg.chunk）— 高频、可丢，SSE 客户端可 catch-up
- `SessionStateChanged` — 低频、不可丢，决定 dashboard/audit 一致性

需要差异化的背压和持久化策略。

## 决策

**每个 RelayEvent 子类声明 `delivery_tier: Literal["lossy", "durable"]`。EventBus dispatch 根据 tier 选择不同背压策略。Durable 事件额外 persist-before-fanout。**

## 理由

1. **不阻塞热路径** — 高频 chunk 事件丢弃不影响正确性
2. **保证审计完整性** — 状态变更/HITL 事件持久化到 events 表
3. **支持 SSE 断点续传** — durable 事件有 monotonic seq，客户端可 replay
4. **与 Redis Streams 天然契合** — durable 事件 XADD 到 stream，跨 worker 可见

## 后果

- 每个新 event 必须选择 tier（默认 lossy 安全但可能漏审计）
- durable_store 故障时 strict_durable=True 会阻止 publish
- events 表需要 retention job 防止无限膨胀

## 关联代码

- `src/gg_relay/core/events.py` — 每个子类的 `delivery_tier` 字段
- `src/gg_relay/core/event_bus.py` — `_dispatch()` tier-aware 逻辑
- `src/gg_relay/store/durable_event.py` — `SqlAlchemyDurableEventStore`
