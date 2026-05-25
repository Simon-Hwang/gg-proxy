---
id: adr-summary
level: ADR
type: adr
title: "ADR 索引"
tags: [architecture, decision, adr]
domain: [architecture, design-decision]
intent:
  - "查看所有架构决策记录"
  - "了解系统为什么这样设计"
source_paths:
  - docs/architecture.md
  - docs/security.md
  - docs/cluster.md
symbols:
  - EventBus
  - Protocol
  - frozen-dataclass
  - optimistic-locking
graph_node_id: adr-summary
token_estimate: 800
summary: >
  gg-relay 架构决策索引：EventBus 唯一 fan-out、Protocol 接口、不可变领域对象和乐观锁并发控制
created: 2026-05-25
updated: 2026-05-25
confidence: high
---

# ADR 索引

| ID | 标题 | 状态 | 关键字 | 一句话摘要 | 全文路径 |
|----|------|------|--------|-----------|----------|
| 001 | EventBus 唯一 fan-out | Accepted | event-bus, decoupling, pub-sub | 所有跨组件通信仅通过 EventBus typed events，禁止直接调用 | ADR/001-eventbus-only-fanout.md |
| 002 | Protocol 接口隔离 | Accepted | protocol, structural-typing, dependency-inversion | 所有跨模块边界用 typing.Protocol，无导入耦合 | ADR/002-protocol-interfaces.md |
| 003 | 乐观锁并发控制 | Accepted | optimistic-locking, version, ConcurrencyError | sessions/hitl 表用 version 列实现乐观锁，竞争时 409 | ADR/003-optimistic-locking.md |
| 004 | Delivery Tier 事件分级 | Accepted | lossy, durable, backpressure | EventBus 事件分 lossy（可丢）和 durable（持久化优先）两级 | ADR/004-delivery-tiers.md |
